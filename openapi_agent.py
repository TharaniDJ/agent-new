"""
OpenAPI Spec Finder Agent - v5

Changes from v4:
- temperature=0 on every LLM call → deterministic navigation decisions
- Step 3 prompt: for GitHub repos, fetch commit history to find last-committed file
  among same-version candidates (GitHub-specific only, guarded carefully)
- Step 4 prompt: YAML preferred, JSON accepted as fallback if no YAML exists
- Programmatic tiebreaker: GitHub Commits API used to rank same-version same-format candidates
- Memory: removed useful_pages and last_search_outcome, added spec_url_history
  (accumulating list of previously found URLs — newest first, max 5)
- Memory hint: shows agent the URL history so it can identify repo/path patterns
"""

import anthropic
import json
import os
import re
import requests
import yaml
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urljoin, urlparse

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────────────────────────────────────

MEMORY_FILE = "search_memory.json"
MAX_URL_HISTORY = 5          # keep the N most recent found URLs per API


def _memory_key(docs_url: str) -> str:
    parsed = urlparse(docs_url)
    return (parsed.netloc + parsed.path).rstrip("/")


def load_memory() -> Dict[str, Any]:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_memory(memory: Dict[str, Any]) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def get_memory_entry(docs_url: str) -> Optional[Dict[str, Any]]:
    return load_memory().get(_memory_key(docs_url))


def update_memory_entry(docs_url: str, new_spec_url: Optional[str], extra: Dict[str, Any]) -> None:
    """
    Update memory for a docs URL.
    - Accumulates spec_url_history (newest first, capped at MAX_URL_HISTORY)
    - Merges extra fields (spec_repo, search_notes, last_found_version)
    - Intentionally does NOT store useful_pages or last_search_outcome
    """
    memory = load_memory()
    key = _memory_key(docs_url)
    existing = memory.get(key, {})

    # Accumulate URL history — newest first, no duplicates
    if new_spec_url:
        history: List[str] = existing.get("spec_url_history", [])
        if new_spec_url in history:
            history.remove(new_spec_url)          # move to front if already present
        history.insert(0, new_spec_url)
        existing["spec_url_history"] = history[:MAX_URL_HISTORY]

    # Merge simple scalar fields
    for k, v in extra.items():
        if v is not None:
            existing[k] = v

    memory[key] = existing
    save_memory(memory)


def build_memory_hint(entry: Dict[str, Any]) -> str:
    """
    Build the natural-language hint appended to the system prompt.
    Shows the agent:
      - Which GitHub repo held the spec previously
      - The last N spec URLs found (so it can see the URL pattern)
      - The last known version (so it knows what to beat)
      - Any search notes from last time
    """
    parts = []

    if entry.get("spec_repo"):
        parts.append(
            f"Previously the spec was found in this repository: {entry['spec_repo']}. "
            "Start there and check for newer releases or files."
        )

    history = entry.get("spec_url_history", [])
    if history:
        url_lines = "\n".join(f"  - {u}" for u in history[:3])
        parts.append(
            f"Previously found spec URLs (newest first — use these to understand "
            f"the repo structure and file naming pattern):\n{url_lines}"
        )

    if entry.get("last_found_version"):
        parts.append(
            f"The last known spec version was {entry['last_found_version']}. "
            "Look for anything newer — but always return the latest regardless."
        )

    if entry.get("search_notes"):
        parts.append(f"Notes from last search: {entry['search_notes']}")

    if not parts:
        return ""

    return (
        "\n\n## Memory from previous search\n"
        + "\n".join(parts)
        + "\n\nUse this as a starting hint only. Always verify you have the LATEST version."
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_raw(url: str, timeout: int = 20) -> Optional[requests.Response]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "OpenAPI-Spec-Finder/5.0"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp
    except Exception as e:
        print(f"  [fetch_raw] {url} → {e}")
        return None


def head_check(url: str, timeout: int = 10) -> bool:
    """Lightweight existence check — no body downloaded."""
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": "OpenAPI-Spec-Finder/5.0"},
            timeout=timeout,
            allow_redirects=True,
        )
        return resp.status_code == 200
    except Exception:
        return False


def github_blob_to_raw(url: str) -> str:
    """Convert a github.com blob URL to raw.githubusercontent.com."""
    url = url.replace("https://github.com/", "https://raw.githubusercontent.com/")
    url = url.replace("/blob/", "/")
    return url


def parse_github_raw_url(raw_url: str) -> Optional[Tuple[str, str, str, str]]:
    """
    Parse a raw.githubusercontent.com URL into (owner, repo, branch, filepath).
    Returns None if not a raw GitHub URL.
    Example:
      https://raw.githubusercontent.com/stripe/openapi/master/openapi.yaml
      → ("stripe", "openapi", "master", "openapi.yaml")
    """
    m = re.match(
        r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)",
        raw_url,
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def github_last_commit_ts(owner: str, repo: str, branch: str, filepath: str) -> Optional[str]:
    """
    Call the GitHub Commits API to get the timestamp of the most recent commit
    that touched `filepath` in `owner/repo` on `branch`.
    Returns an ISO timestamp string or None on failure.
    No auth token required for public repos (60 req/hr unauthenticated).
    """
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?path={filepath}&sha={branch}&per_page=1"
    )
    try:
        resp = requests.get(
            api_url,
            headers={
                "User-Agent": "OpenAPI-Spec-Finder/5.0",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        commits = resp.json()
        if not commits:
            return None
        return commits[0]["commit"]["committer"]["date"]
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PROGRAMMATIC VALIDATORS  (called ONCE after LLM loop)
# ─────────────────────────────────────────────────────────────────────────────

def parse_spec(text: str) -> Optional[Dict]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass
    return None


def validate_openapi_spec(url: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "valid": False, "version": None,
        "title": None, "format": None, "error": None,
    }
    resp = fetch_raw(url)
    if resp is None:
        result["error"] = "Could not fetch URL"
        return result

    text = resp.text.strip()
    ct = resp.headers.get("content-type", "")

    if url.endswith((".yaml", ".yml")) or "yaml" in ct:
        result["format"] = "yaml"
    elif url.endswith(".json") or "json" in ct:
        result["format"] = "json"
    else:
        result["format"] = "yaml" if text.startswith(("openapi:", "swagger:")) else "json"

    data = parse_spec(text)
    if data is None:
        result["error"] = "Could not parse as JSON or YAML"
        return result
    if not isinstance(data, dict):
        result["error"] = "Parsed content is not a dict"
        return result

    if "openapi" in data:
        result["valid"] = True
        result["version"] = str(data["openapi"])
    elif "swagger" in data:
        result["valid"] = True
        result["version"] = str(data["swagger"])
    else:
        result["error"] = "No 'openapi' or 'swagger' key found"
        return result

    info = data.get("info", {})
    result["title"] = info.get("title") if isinstance(info, dict) else None
    return result


def _ver_tuple(ver_str: Optional[str]) -> tuple:
    """Convert "3.1.0" → (3, 1, 0) for comparison. Unknown → (0,)."""
    if not ver_str:
        return (0,)
    try:
        return tuple(int(x) for x in ver_str.split("."))
    except ValueError:
        return (0,)


def best_validated_url(candidates: List[str]) -> Optional[tuple]:
    """
    1. HEAD pre-check every candidate (cheap, catches hallucinated URLs).
    2. Full parse + validate survivors.
    3. Rank by: highest OpenAPI version → YAML over JSON → most recently
       committed (GitHub Commits API, only called as tiebreaker on same-version
       same-format pairs in GitHub raw URLs).
    Returns (best_url, validation_result) or None.
    """
    valid_results: List[Tuple[str, Dict[str, Any]]] = []

    for url in candidates:
        url = url.strip()
        if not url or not url.startswith("http"):
            continue

        # Convert github.com blob → raw
        if "github.com" in url and "/blob/" in url:
            url = github_blob_to_raw(url)

        print(f"  [head-check] {url}")
        if not head_check(url):
            print(f"    ✗ not reachable (skipping full download)")
            continue

        print(f"  [validate]   {url}")
        vr = validate_openapi_spec(url)
        if vr["valid"]:
            valid_results.append((url, vr))
            print(f"    ✓ valid  version={vr['version']}  format={vr['format']}  title={vr['title']}")
        else:
            print(f"    ✗ invalid: {vr['error']}")

    if not valid_results:
        return None

    # ── Tiebreaker: GitHub last-commit timestamp ──────────────────────────────
    # Only call the Commits API when two or more candidates share the same
    # OpenAPI version AND the same format AND are both raw GitHub URLs.
    # This is the expensive-but-accurate tiebreaker for repos like DocuSign
    # where multiple versioned files coexist.
    commit_cache: Dict[str, Optional[str]] = {}

    def last_commit(url: str) -> Optional[str]:
        if url not in commit_cache:
            parsed = parse_github_raw_url(url)
            if parsed:
                owner, repo, branch, filepath = parsed
                ts = github_last_commit_ts(owner, repo, branch, filepath)
                commit_cache[url] = ts
                if ts:
                    print(f"  [commit-ts]  {url.split('/')[-1]} → {ts}")
            else:
                commit_cache[url] = None
        return commit_cache[url]

    def sort_key(item: Tuple[str, Dict]) -> tuple:
        url, vr = item
        ver_score = _ver_tuple(vr.get("version"))
        fmt_score = 1 if vr["format"] == "yaml" else 0
        # Commit timestamp as tiebreaker — only fetched when needed
        ts = last_commit(url) or "0000-00-00T00:00:00Z"
        return (ver_score, fmt_score, ts)

    valid_results.sort(key=sort_key, reverse=True)
    return valid_results[0]


# ─────────────────────────────────────────────────────────────────────────────
# FETCH PAGE TOOL
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> Dict[str, Any]:
    try:
        resp = fetch_raw(url)
        if resp is None:
            return {"url": url, "type": "error", "error": "Request failed"}

        ct = resp.headers.get("content-type", "")
        if "json" in ct or url.endswith(".json"):
            return {"url": url, "type": "json", "content": resp.text[:40000]}
        if "yaml" in ct or url.endswith((".yaml", ".yml")):
            return {"url": url, "type": "yaml", "content": resp.text[:40000]}

        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)

        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"])
            low = full.lower()
            if full in seen:
                continue
            if any(kw in low for kw in [
                "openapi", "swagger", "api-spec", "apispec",
                ".json", ".yaml", ".yml", "raw.githubusercontent",
                "spec3", "api-reference", "rest-api-description",
                "releases", "tags", "tree/main", "tree/master", "/defs/",
                "commits", "blob/main", "blob/master",
            ]):
                seen.add(full)
                links.append({"text": a.get_text(strip=True)[:80], "href": full})

        return {
            "url": url,
            "type": "html",
            "content": text[:15000],
            "relevant_links": links[:40],
        }
    except Exception as e:
        return {"url": url, "type": "error", "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are an expert OpenAPI spec finder agent. Your ONLY job is to find the direct URL(s) to an API's LATEST official OpenAPI specification file (JSON or YAML).

## Step-by-step strategy

### Step 1 — Read the official docs page FIRST
Always start by fetching the starting documentation URL given to you.
Carefully read ALL links on that page. API documentation pages often directly mention or link to their OpenAPI spec — look for:
- Text like "OpenAPI Specification", "Swagger", "Download spec", "API spec", "generated from our OpenAPI spec"
- Any link containing: openapi, swagger, .yaml, .yml, .json, spec, defs, raw.githubusercontent

### Step 2 — Construct raw GitHub URLs immediately (do NOT keep fetching tree pages)
If you find a GitHub repository link or a file path reference:
- DO NOT fetch github.com/owner/repo/tree/branch/path pages — these are HTML pages, not spec files
- Instead, IMMEDIATELY construct the raw URL:
  github.com/owner/repo/blob/branch/path/file.yaml → raw.githubusercontent.com/owner/repo/branch/path/file.yaml
- Report the constructed raw URL as a candidate right away

### Step 3 — Picking the latest version (read carefully)

**3a — Version number in filename takes priority**
When multiple versioned files exist (e.g. swagger-v2.json and swagger-v2.1.json):
- Always prefer the highest version number (v2.1 > v2, v3 > v2)
- Parse the version from the FILENAME itself (e.g. -v2.1.json = version 2.1)
- Report ALL versioned variants as candidates — the validator will pick the best

**3b — For GitHub-hosted spec repositories, check the commit history**
This applies ONLY when the specs live in a dedicated GitHub repository (like
github.com/stripe/openapi or github.com/docusign/OpenAPI-Specifications).
Do NOT apply this to general documentation websites.
- If you find a GitHub repo with multiple spec files of the same version,
  fetch the repo's commit list page: github.com/owner/repo/commits/main
  (or /commits/master if main does not exist)
- Read the commit timestamps to identify which spec file was committed most recently
- Include the most recently committed file in your candidates
- This ensures you get the file that was actually updated last, not just the one with
  the highest version suffix

**3c — GitHub Releases / Tags as a cross-check**
- If the repo has a Releases page, check it to confirm the latest tagged release
- The latest release tag is a strong signal for which files are current

### Step 4 — Format preference
- Always prefer YAML over JSON when both exist at the same version
- If only JSON exists, JSON is perfectly acceptable — report it
- Never skip a valid JSON spec just because YAML is not available

## Efficiency rules
- Fetch the starting docs page first — read it carefully before going anywhere else
- Fetch at MOST 4 pages total (counting commits/releases pages)
- Never fetch github.com tree/blob pages to read file contents — construct raw URLs directly
- Never fetch the same URL twice
- Report ALL plausible candidates — the validator handles the final selection

## Output format — output EXACTLY this block when done:

SPEC_CANDIDATES:
<url1>
<url2>
SEARCH_NOTES: <one sentence about where/how you found it>
SPEC_REPO: <GitHub or source repo URL if applicable, else omit this line>

If no public spec exists after thorough search:
NO_SPEC_FOUND
"""

TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetches a web page and returns its text content plus relevant links. "
            "Use for: documentation pages, GitHub repo home pages, GitHub commits pages, "
            "GitHub releases pages. "
            "Do NOT use for GitHub tree/blob file-listing pages — construct raw.githubusercontent.com URLs directly instead. "
            "Do NOT fetch the same URL twice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to fetch"}
            },
            "required": ["url"],
        },
    }
]


# ─────────────────────────────────────────────────────────────────────────────
# AGENT
# ─────────────────────────────────────────────────────────────────────────────

class OpenAPIAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def _parse_agent_output(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "candidates": [],
            "search_notes": None,
            "spec_repo": None,
        }
        if "SPEC_CANDIDATES:" not in text:
            return result

        after = text.split("SPEC_CANDIDATES:", 1)[1]
        for line in after.splitlines():
            line = line.strip()
            if line.startswith("http"):
                result["candidates"].append(line)
            elif line.startswith("SEARCH_NOTES:"):
                result["search_notes"] = line.replace("SEARCH_NOTES:", "").strip()
            elif line.startswith("SPEC_REPO:"):
                result["spec_repo"] = line.replace("SPEC_REPO:", "").strip()
        return result

    def run(
        self,
        starting_url: str,
        api_name: str = "",
        max_iterations: int = 6,
        target_title: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Main agent loop. Always does a fresh search for the latest spec.
        Memory guides navigation strategy but never skips the search.
        temperature=0 for deterministic navigation decisions.

        Returns:
        {
            "spec_url":       str,
            "version":        str | None,
            "format":         "yaml" | "json" | None,
            "title":          str | None,
            "is_new_version": bool,
        }
        or None if not found.
        """
        mem_entry = get_memory_entry(starting_url)
        memory_hint = build_memory_hint(mem_entry) if mem_entry else ""
        last_known_version = (mem_entry or {}).get("last_found_version")

        if memory_hint:
            print(f"  [memory] Previous search data found — guiding agent navigation")
            if last_known_version:
                print(f"  [memory] Last known version: {last_known_version}")
            history = (mem_entry or {}).get("spec_url_history", [])
            if history:
                print(f"  [memory] URL history: {history[0]}")

        system_prompt = BASE_SYSTEM_PROMPT + memory_hint

        user_msg = f"Find the LATEST OpenAPI spec URL starting from: {starting_url}\n"
        if api_name:
            user_msg += f"API name: {api_name}\n"
        if target_title:
            user_msg += (
                f"Target spec: look specifically for the spec titled '{target_title}' "
                f"among the available specs on this page.\n"
            )
        if last_known_version:
            user_msg += (
                f"The last known version was {last_known_version}. "
                "Check if a newer version exists — but always return the latest regardless.\n"
            )
        user_msg += (
            "\nIMPORTANT: Fetch the starting docs page first and read ALL links carefully "
            "before going elsewhere. Fetch at most 4 pages total. "
            "Output SPEC_CANDIDATES: as soon as you have plausible URLs."
        )

        messages = [{"role": "user", "content": user_msg}]
        fetched_urls: set = set()
        parsed_output: Optional[Dict[str, Any]] = None

        for iteration in range(max_iterations):
            print(f"\n--- Iteration {iteration + 1} ---")

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                temperature=0,          # ← deterministic: always picks highest-prob token
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            print(f"Stop reason: {response.stop_reason}")

            found_candidates = False
            for block in response.content:
                if block.type == "text":
                    print(f"Claude: {block.text[:500]}")
                    if "SPEC_CANDIDATES:" in block.text:
                        parsed_output = self._parse_agent_output(block.text)
                        if parsed_output["candidates"]:
                            found_candidates = True
                            break
                    if "NO_SPEC_FOUND" in block.text:
                        print("  Agent reports no spec found.")
                        return None

            if found_candidates:
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use" and block.name == "fetch_page":
                        url = block.input["url"]
                        if url in fetched_urls:
                            content = json.dumps({
                                "error": "Already fetched this URL. Choose a different one."
                            })
                        else:
                            fetched_urls.add(url)
                            print(f"  Fetching: {url}")
                            page = fetch_page(url)
                            content = json.dumps(page, indent=2)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                        })
                messages.append({"role": "user", "content": tool_results})

            elif response.stop_reason == "end_turn":
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": (
                        "Conclude now. Output SPEC_CANDIDATES: followed by URLs and metadata lines, "
                        "or output NO_SPEC_FOUND."
                    ),
                })

        # ── Programmatic validation — runs ONCE outside LLM loop ──
        if not parsed_output or not parsed_output["candidates"]:
            print("  No candidates collected from agent.")
            return None

        candidates = parsed_output["candidates"]
        print(f"\n[validation] {len(candidates)} candidate(s) — HEAD pre-checking then validating…")

        best = best_validated_url(candidates)
        if best is None:
            print("  No candidates passed validation.")
            return None

        best_url, vr = best

        # ── Version comparison ──
        is_new_version = False
        if last_known_version and vr["version"]:
            try:
                old_parts = _ver_tuple(last_known_version)
                new_parts = _ver_tuple(vr["version"])
                is_new_version = new_parts > old_parts
            except Exception:
                is_new_version = vr["version"] != last_known_version

        # ── Update memory ──
        update_memory_entry(
            docs_url=starting_url,
            new_spec_url=best_url,
            extra={
                "last_found_version": vr["version"],
                "spec_repo": parsed_output.get("spec_repo"),
                "search_notes": parsed_output.get("search_notes"),
            },
        )
        print(f"  [memory] Updated for {_memory_key(starting_url)}")

        return {
            "spec_url": best_url,
            "version": vr["version"],
            "format": vr["format"],
            "title": vr["title"],
            "is_new_version": is_new_version,
        }
