"""
OpenAPI Spec Finder Agent - v6

Key changes from v5:
- Memory is now keyed per (docs_url, api_name) so every spec gets its own entry,
  even when multiple specs share the same docs_url (e.g. all HubSpot entries).
- The agent is given the official documentation URL and must discover the spec
  itself — GitHub repo URLs are no longer used as starting points in API_DOCS.
- New generic GitHub repository reasoning: the agent learns to find the highest
  rollout/release folder and prefers standard vN versioning over date-based
  folders (e.g. v3, v4 > 2026-09, 2026-03).  This reasoning is entirely
  derived from what the agent observes in the repo, not hard-coded rules.
- fetch_directory tool added: fetches a GitHub API directory listing as JSON
  so the agent can enumerate folders without scraping HTML tree pages.
- temperature=0 retained for determinism.
- Programmatic tiebreaker (GitHub Commits API) retained.
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
# MEMORY  — one entry per (docs_url, api_name) pair
# ─────────────────────────────────────────────────────────────────────────────

MEMORY_FILE = "search_memory.json"
MAX_URL_HISTORY = 5


def _memory_key(docs_url: str, api_name: str) -> str:
    """
    Unique key per spec, even when docs_url is shared across many specs
    (e.g. a GitHub monorepo hosting dozens of independent API specs).
    """
    parsed = urlparse(docs_url)
    base = (parsed.netloc + parsed.path).rstrip("/")
    # Normalise api_name: lowercase, collapse whitespace → underscores
    name_slug = re.sub(r"\s+", "_", api_name.strip().lower())
    return f"{base}::{name_slug}"


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


def get_memory_entry(docs_url: str, api_name: str) -> Optional[Dict[str, Any]]:
    return load_memory().get(_memory_key(docs_url, api_name))


def update_memory_entry(
    docs_url: str,
    api_name: str,
    new_spec_url: Optional[str],
    extra: Dict[str, Any],
) -> None:
    memory = load_memory()
    key = _memory_key(docs_url, api_name)
    existing = memory.get(key, {})

    if new_spec_url:
        history: List[str] = existing.get("spec_url_history", [])
        if new_spec_url in history:
            history.remove(new_spec_url)
        history.insert(0, new_spec_url)
        existing["spec_url_history"] = history[:MAX_URL_HISTORY]

    for k, v in extra.items():
        if v is not None:
            existing[k] = v

    memory[key] = existing
    save_memory(memory)


def build_memory_hint(entry: Dict[str, Any]) -> str:
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

HEADERS = {"User-Agent": "OpenAPI-Spec-Finder/6.0"}


def fetch_raw(url: str, timeout: int = 20) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        print(f"  [fetch_raw] {url} → {e}")
        return None


def head_check(url: str, timeout: int = 10) -> bool:
    try:
        resp = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp.status_code == 200
    except Exception:
        return False


def github_blob_to_raw(url: str) -> str:
    url = url.replace("https://github.com/", "https://raw.githubusercontent.com/")
    return url.replace("/blob/", "/")


def parse_github_raw_url(raw_url: str) -> Optional[Tuple[str, str, str, str]]:
    m = re.match(
        r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)",
        raw_url,
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def github_last_commit_ts(owner: str, repo: str, branch: str, filepath: str) -> Optional[str]:
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?path={filepath}&sha={branch}&per_page=1"
    )
    try:
        resp = requests.get(
            api_url,
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
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
# GITHUB CONTENTS API  — enumerate folders without scraping HTML
# ─────────────────────────────────────────────────────────────────────────────

def github_list_directory(owner: str, repo: str, path: str, branch: str = "main") -> Optional[List[Dict]]:
    """
    Call the GitHub Contents API to list a directory.
    Returns a list of {name, type, path, download_url} dicts, or None on failure.
    Tries 'main' then 'master' automatically.
    """
    for br in ([branch] if branch not in ("main", "master") else ["main", "master"]):
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={br}"
        try:
            resp = requests.get(
                url,
                headers={**HEADERS, "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return None


def _parse_github_repo_url(url: str) -> Optional[Tuple[str, str]]:
    """Extract (owner, repo) from a github.com URL."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PROGRAMMATIC VALIDATORS
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

    if url.split("?")[0].endswith((".yaml", ".yml")) or "yaml" in ct:
        result["format"] = "yaml"
    elif url.split("?")[0].endswith(".json") or "json" in ct:
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
    if not ver_str:
        return (0,)
    try:
        return tuple(int(x) for x in ver_str.split("."))
    except ValueError:
        return (0,)


def best_validated_url(candidates: List[str]) -> Optional[tuple]:
    valid_results: List[Tuple[str, Dict[str, Any]]] = []

    for url in candidates:
        url = url.strip()
        if not url or not url.startswith("http"):
            continue

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
        ts = last_commit(url) or "0000-00-00T00:00:00Z"
        return (ver_score, fmt_score, ts)

    valid_results.sort(key=sort_key, reverse=True)
    return valid_results[0]


# ─────────────────────────────────────────────────────────────────────────────
# TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> Dict[str, Any]:
    """Fetch a web page; return text + relevant links."""
    try:
        resp = fetch_raw(url)
        if resp is None:
            return {"url": url, "type": "error", "error": "Request failed"}

        ct = resp.headers.get("content-type", "")
        if "json" in ct or url.split("?")[0].endswith(".json"):
            return {"url": url, "type": "json", "content": resp.text[:40000]}
        if "yaml" in ct or url.split("?")[0].endswith((".yaml", ".yml")):
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
                "commits", "blob/main", "blob/master", "github.com",
            ]):
                seen.add(full)
                links.append({"text": a.get_text(strip=True)[:80], "href": full})

        return {
            "url": url,
            "type": "html",
            "content": text[:15000],
            "relevant_links": links[:50],
        }
    except Exception as e:
        return {"url": url, "type": "error", "error": str(e)}


def list_github_directory(owner: str, repo: str, path: str, branch: str = "main") -> Dict[str, Any]:
    """
    Use the GitHub Contents API to list a directory.
    Returns folder names and file names with download URLs.
    Much more reliable than scraping HTML tree pages.
    """
    items = github_list_directory(owner, repo, path, branch)
    if items is None:
        return {
            "owner": owner, "repo": repo, "path": path,
            "error": "Could not list directory — check owner/repo/path/branch",
        }

    dirs = sorted([i["name"] for i in items if i["type"] == "dir"])
    files = [
        {"name": i["name"], "download_url": i.get("download_url")}
        for i in items if i["type"] == "file"
    ]
    return {
        "owner": owner,
        "repo": repo,
        "path": path,
        "directories": dirs,
        "files": files,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are an expert OpenAPI spec finder agent. Your ONLY job is to find the direct URL to an API's LATEST official OpenAPI specification file (JSON or YAML).

You have two tools:
1. `fetch_page`  — fetch any web page (docs, GitHub repo home, GitHub releases/commits page)
2. `list_github_directory` — list a GitHub directory via the Contents API (returns folder and file names precisely)

---

## Step-by-step strategy

### Step 1 — Start from the official documentation page
Always fetch the starting URL first.  Read every link carefully.
Look for anything mentioning: OpenAPI, Swagger, spec, download spec, .yaml, .yml, .json, GitHub.

### Step 2 — Follow the trail to a GitHub repository (if applicable)
Many APIs host their specs in a GitHub repository linked from the docs.
Once you find a GitHub repo link, use `list_github_directory` — NOT `fetch_page` for tree pages.

### Step 3 — Navigate the repository structure intelligently

**Finding the right folder:**
Use `list_github_directory` repeatedly to explore the repo hierarchy until you reach the directory that contains the actual spec file.  Common layouts:
  - Flat:       owner/repo → openapi.yaml
  - By product: owner/repo/specs/ProductName/ → openapi.yaml
  - Rollouts:   owner/repo/PublicApiSpecs/Category/Product/Rollouts/{rollout_id}/{version}/ → spec.json

**Choosing the right rollout / release folder (when multiple exist):**
When you see numbered sub-folders (e.g. 120891, 130902, 144903):
  - These are rollout or release identifiers.
  - List them and pick the HIGHEST number — that is always the most recent rollout.
  - Do NOT guess — call `list_github_directory` to enumerate the actual folder names.

**Choosing the right version sub-folder (when multiple exist inside a rollout):**
After entering the highest rollout folder you may see sub-folders like:
  v3, v4, 2025-09, 2026-03, 2026-09, etc.
  - ALWAYS prefer a standard semantic version folder (v3, v4, v5 …) over a date-based folder.
  - Among semantic version folders, pick the HIGHEST (v4 > v3 > v2).
  - Only fall back to date-based folders if NO semantic version folder exists.
  - Among date-based folders, pick the one with the latest date.

**File format preference:**
  - Prefer YAML (.yaml / .yml) over JSON when both exist at the same version.
  - JSON is fine if YAML is absent.

### Step 4 — Construct raw download URLs
For GitHub files, construct the raw URL directly:
  github.com/owner/repo/blob/branch/path/file.yaml
  → raw.githubusercontent.com/owner/repo/branch/path/file.yaml

Report ALL plausible candidates — the validator picks the best.

---

## Efficiency rules
- Fetch the docs page first; read every link before going elsewhere.
- Use `list_github_directory` for GitHub path exploration (precise and cheap).
- Use `fetch_page` for docs pages, GitHub repo home pages, and release/commit pages.
- Fetch at most 6 pages/directories total.
- Never fetch the same URL twice.

---

## Output — emit EXACTLY this block when done:

SPEC_CANDIDATES:
<url1>
<url2>
SEARCH_NOTES: <one sentence — where/how you found it and which folder strategy you used>
SPEC_REPO: <GitHub repo URL if applicable, else omit>

If no public spec exists after a thorough search:
NO_SPEC_FOUND
SEARCH_NOTES: <why not found>
"""

TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetch a web page and return its text content plus relevant links. "
            "Use for: official API documentation pages, GitHub repository home pages, "
            "GitHub releases pages, GitHub commits pages. "
            "Do NOT use for GitHub tree/blob file-listing pages — use list_github_directory instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_github_directory",
        "description": (
            "List the contents of a directory in a GitHub repository using the Contents API. "
            "Returns directory names and file names with download URLs. "
            "Use this whenever you need to enumerate folders or files in a GitHub repo "
            "— it is precise and does not require scraping HTML. "
            "Example: to explore owner='stripe', repo='openapi', path='openapi', branch='master'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner":  {"type": "string", "description": "GitHub repository owner"},
                "repo":   {"type": "string", "description": "GitHub repository name"},
                "path":   {"type": "string", "description": "Directory path inside the repo (use '' or '.' for root)"},
                "branch": {"type": "string", "description": "Branch name (default: main)", "default": "main"},
            },
            "required": ["owner", "repo", "path"],
        },
    },
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
        max_iterations: int = 10,
        target_title: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Main agent loop. Memory is per (starting_url, api_name) pair.
        Returns:
          {"spec_url", "version", "format", "title", "is_new_version"}
        or None.
        """
        mem_entry = get_memory_entry(starting_url, api_name)
        memory_hint = build_memory_hint(mem_entry) if mem_entry else ""
        last_known_version = (mem_entry or {}).get("last_found_version")

        if memory_hint:
            print(f"  [memory] Previous entry found for '{api_name}'")
            if last_known_version:
                print(f"  [memory] Last known version: {last_known_version}")

        system_prompt = BASE_SYSTEM_PROMPT + memory_hint

        user_msg = f"Find the LATEST OpenAPI spec URL starting from: {starting_url}\n"
        if api_name:
            user_msg += f"API name: {api_name}\n"
        if target_title:
            user_msg += (
                f"Target spec title: '{target_title}' "
                f"(look specifically for this spec among multiple available).\n"
            )
        if last_known_version:
            user_msg += (
                f"Last known version: {last_known_version}. "
                "Check if a newer version exists — but always return the latest regardless.\n"
            )
        user_msg += (
            "\nIMPORTANT: Start by fetching the docs page. "
            "Use list_github_directory to explore GitHub repos. "
            "Output SPEC_CANDIDATES: as soon as you have plausible direct file URLs."
        )

        messages = [{"role": "user", "content": user_msg}]
        fetched_urls: set = set()
        listed_dirs: set = set()
        parsed_output: Optional[Dict[str, Any]] = None

        for iteration in range(max_iterations):
            print(f"\n--- Iteration {iteration + 1} ---")

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                temperature=0,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            print(f"Stop reason: {response.stop_reason}")

            found_candidates = False
            for block in response.content:
                if block.type == "text":
                    print(f"Claude: {block.text[:600]}")
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
                    if block.type != "tool_use":
                        continue

                    if block.name == "fetch_page":
                        url = block.input["url"]
                        if url in fetched_urls:
                            content = json.dumps({"error": "Already fetched this URL."})
                        else:
                            fetched_urls.add(url)
                            print(f"  [fetch_page] {url}")
                            content = json.dumps(fetch_page(url), indent=2)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                        })

                    elif block.name == "list_github_directory":
                        owner  = block.input["owner"]
                        repo   = block.input["repo"]
                        path   = block.input.get("path", "")
                        branch = block.input.get("branch", "main")
                        dir_key = f"{owner}/{repo}/{path}@{branch}"
                        if dir_key in listed_dirs:
                            content = json.dumps({"error": "Already listed this directory."})
                        else:
                            listed_dirs.add(dir_key)
                            print(f"  [list_dir]   {owner}/{repo}/{path} (branch={branch})")
                            content = json.dumps(list_github_directory(owner, repo, path, branch), indent=2)
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
                        "Conclude now. Output SPEC_CANDIDATES: followed by direct file URLs, "
                        "or output NO_SPEC_FOUND."
                    ),
                })

        # ── Programmatic validation ──────────────────────────────────────────
        if not parsed_output or not parsed_output["candidates"]:
            print("  No candidates collected from agent.")
            return None

        candidates = parsed_output["candidates"]
        print(f"\n[validation] {len(candidates)} candidate(s) — validating…")

        best = best_validated_url(candidates)
        if best is None:
            print("  No candidates passed validation.")
            return None

        best_url, vr = best

        is_new_version = False
        if last_known_version and vr["version"]:
            try:
                is_new_version = _ver_tuple(vr["version"]) > _ver_tuple(last_known_version)
            except Exception:
                is_new_version = vr["version"] != last_known_version

        update_memory_entry(
            docs_url=starting_url,
            api_name=api_name,
            new_spec_url=best_url,
            extra={
                "last_found_version": vr["version"],
                "spec_repo": parsed_output.get("spec_repo"),
                "search_notes": parsed_output.get("search_notes"),
            },
        )
        print(f"  [memory] Saved entry for '{api_name}'")

        return {
            "spec_url": best_url,
            "version": vr["version"],
            "format": vr["format"],
            "title": vr["title"],
            "is_new_version": is_new_version,
        }
