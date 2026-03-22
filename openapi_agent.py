"""
OpenAPI Spec Finder Agent - v4
Fixes in this version:
- Agent now constructs raw GitHub URLs immediately instead of fetching more tree pages
- Agent told to pick highest version suffix in filenames (v2.1 > v2)
- HEAD-check helper validates constructed URLs before they are reported as candidates
- Prompt tells agent to read links from the official docs page first before navigating elsewhere
- Candid: supports extracting multiple specific specs from one docs URL
- Hallucinated URLs are caught before validation via HEAD pre-check
"""

import anthropic
import json
import os
import requests
import yaml
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin, urlparse

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────────────────────────────────────

MEMORY_FILE = "search_memory.json"


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


def update_memory_entry(docs_url: str, entry: Dict[str, Any]) -> None:
    memory = load_memory()
    key = _memory_key(docs_url)
    existing = memory.get(key, {})
    existing.update(entry)
    existing["last_searched"] = datetime.now(timezone.utc).isoformat()
    memory[key] = existing
    save_memory(memory)


def build_memory_hint(entry: Dict[str, Any]) -> str:
    parts = []
    if entry.get("spec_repo"):
        parts.append(
            f"Previously the spec was found in this repository: {entry['spec_repo']}. "
            "Start there and check for the latest release or version."
        )
    if entry.get("useful_pages"):
        pages = "\n".join(f"  - {p}" for p in entry["useful_pages"][:5])
        parts.append(f"These pages were useful last time:\n{pages}")
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
            headers={"User-Agent": "OpenAPI-Spec-Finder/4.0"},
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
            headers={"User-Agent": "OpenAPI-Spec-Finder/4.0"},
            timeout=timeout,
            allow_redirects=True,
        )
        return resp.status_code == 200
    except Exception:
        return False


def github_blob_to_raw(url: str) -> str:
    """Convert a github.com blob URL to raw.githubusercontent.com."""
    # https://github.com/owner/repo/blob/branch/path → https://raw.githubusercontent.com/owner/repo/branch/path
    url = url.replace("https://github.com/", "https://raw.githubusercontent.com/")
    url = url.replace("/blob/", "/")
    return url


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


def best_validated_url(candidates: List[str]) -> Optional[tuple]:
    """
    Pre-check with HEAD before full download, to avoid wasting time on hallucinated URLs.
    Returns (best_url, validation_result) or None.
    Prefers: highest OpenAPI version → YAML over JSON.
    """
    valid_results = []
    for url in candidates:
        url = url.strip()
        if not url or not url.startswith("http"):
            continue

        # Convert any github blob URLs to raw first
        if "github.com" in url and "/blob/" in url:
            url = github_blob_to_raw(url)

        # HEAD pre-check — skips hallucinated URLs cheaply
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

    def sort_key(item):
        _url, vr = item
        fmt_score = 1 if vr["format"] == "yaml" else 0
        ver = vr["version"] or "0"
        try:
            ver_score = float(ver.split(".")[0])
        except ValueError:
            ver_score = 0
        return (ver_score, fmt_score)

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

### Step 3 — Pick the highest version
When multiple versioned files exist (e.g. swagger-v2.json and swagger-v2.1.json):
- Always prefer the highest version number (v2.1 > v2, v3 > v2)
- Look for the version number in the FILENAME itself (e.g. -v2.1.json means API version 2.1)
- Also check GitHub Releases/Tags for the latest release tag

### Step 4 — Prefer YAML over JSON at the same version

## Efficiency rules
- Fetch the starting docs page first — read it carefully before going anywhere else
- Fetch at MOST 4 pages total
- Never fetch github.com tree/blob pages to "see" files — construct raw URLs directly instead
- Never fetch the same URL twice

## Output format — output EXACTLY this block when done:

SPEC_CANDIDATES:
<url1>
<url2>
SEARCH_NOTES: <one sentence about where/how you found it>
SPEC_REPO: <GitHub or source repo URL if applicable, else omit>
USEFUL_PAGES: <comma-separated pages that helped>

If no public spec exists after thorough search:
NO_SPEC_FOUND
"""

TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetches a web page and returns its text content plus relevant links. "
            "For GitHub tree/blob pages, do NOT fetch — instead construct raw.githubusercontent.com URLs directly. "
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
            "useful_pages": [],
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
            elif line.startswith("USEFUL_PAGES:"):
                pages_raw = line.replace("USEFUL_PAGES:", "").strip()
                result["useful_pages"] = [p.strip() for p in pages_raw.split(",") if p.strip()]
        return result

    def run(
        self,
        starting_url: str,
        api_name: str = "",
        max_iterations: int = 6,
        target_title: Optional[str] = None,   # for multi-spec APIs like Candid
    ) -> Optional[Dict[str, Any]]:
        """
        Main agent loop. Always does a fresh search for the latest spec.
        Memory guides navigation strategy, never skips the search.

        target_title: if set, the agent is told to find a specific named spec
                      (used when one docs URL has multiple specs, e.g. Candid).

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
            "\nIMPORTANT: Fetch the starting docs page first and read ALL links carefully before going elsewhere. "
            "Fetch at most 4 pages total. Output SPEC_CANDIDATES: as soon as you have plausible URLs."
        )

        messages = [{"role": "user", "content": user_msg}]
        fetched_urls: set = set()
        parsed_output: Optional[Dict[str, Any]] = None

        for iteration in range(max_iterations):
            print(f"\n--- Iteration {iteration + 1} ---")

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
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
                        update_memory_entry(starting_url, {"last_search_outcome": "not_found"})
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
                            content = json.dumps({"error": "Already fetched this URL. Choose a different one."})
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
                old_parts = [int(x) for x in last_known_version.split(".")]
                new_parts = [int(x) for x in vr["version"].split(".")]
                is_new_version = new_parts > old_parts
            except ValueError:
                is_new_version = vr["version"] != last_known_version

        # ── Update memory ──
        memory_update: Dict[str, Any] = {
            "last_found_version": vr["version"],
            "last_found_url": best_url,
            "last_search_outcome": "found",
        }
        if parsed_output.get("spec_repo"):
            memory_update["spec_repo"] = parsed_output["spec_repo"]
        if parsed_output.get("useful_pages"):
            memory_update["useful_pages"] = parsed_output["useful_pages"]
        if parsed_output.get("search_notes"):
            memory_update["search_notes"] = parsed_output["search_notes"]

        update_memory_entry(starting_url, memory_update)
        print(f"  [memory] Updated for {_memory_key(starting_url)}")

        return {
            "spec_url": best_url,
            "version": vr["version"],
            "format": vr["format"],
            "title": vr["title"],
            "is_new_version": is_new_version,
        }
