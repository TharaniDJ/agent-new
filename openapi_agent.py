"""
OpenAPI Spec Finder Agent - v3
- Memory-guided search: remembers HOW to find specs, not the URL itself
- Always does a fresh search for the latest version
- Programmatic validation happens once at the end (not mid-loop)
- YAML preferred over JSON, latest OpenAPI version preferred
- check_frequency_days field supported but INACTIVE until tested (see TODO)
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
# MEMORY  (search_memory.json)
# Stores HOW we found specs previously — not the spec URL itself.
# Every run still does a fresh search; memory just guides navigation.
# ─────────────────────────────────────────────────────────────────────────────

MEMORY_FILE = "search_memory.json"


def _memory_key(docs_url: str) -> str:
    """Normalise a docs URL into a stable memory key."""
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
    """
    Build a natural-language hint for the agent from a memory entry.
    Tells the agent WHERE to start looking, not WHAT URL to return.
    """
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
            "Look for anything newer than this — but always return the latest regardless."
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
# PROGRAMMATIC VALIDATORS  (called ONCE after LLM loop, never inside it)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_raw(url: str, timeout: int = 20) -> Optional[requests.Response]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "OpenAPI-Spec-Finder/3.0"},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp
    except Exception as e:
        print(f"  [fetch_raw] {url} → {e}")
        return None


def parse_spec(text: str) -> Optional[Dict]:
    """Try JSON then YAML; return parsed dict or None."""
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
    """
    Fetch url, parse it, confirm it is a valid OpenAPI/Swagger spec.
    Returns:
        {
            "valid":   bool,
            "version": str | None,   # e.g. "3.1.0" or "2.0"
            "title":   str | None,
            "format":  "yaml" | "json" | None,
            "error":   str | None,
        }
    """
    result: Dict[str, Any] = {
        "valid": False,
        "version": None,
        "title": None,
        "format": None,
        "error": None,
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
    Validate each candidate URL.
    Returns (best_url, validation_result) tuple, or None.
    Prefers: highest OpenAPI version → YAML over JSON.
    """
    valid_results = []
    for url in candidates:
        url = url.strip()
        if not url or not url.startswith("http"):
            continue
        print(f"  [validate] Checking {url}")
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
# WEB HELPERS  (used by the agent's fetch_page tool)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> Dict[str, Any]:
    """Fetch a webpage and return structured content for the LLM."""
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

        base = url
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(base, href)
            low = full.lower()
            if full in seen:
                continue
            if any(
                kw in low
                for kw in [
                    "openapi", "swagger", "api-spec", "apispec",
                    ".json", ".yaml", ".yml", "raw.githubusercontent",
                    "spec3", "api-reference", "rest-api-description",
                    "releases", "tags", "tree/main", "tree/master",
                ]
            ):
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

BASE_SYSTEM_PROMPT = """You are an expert OpenAPI spec finder agent. Your ONLY job is to locate the direct URL(s) to an API's LATEST official OpenAPI specification file (JSON or YAML).

## Strategy (follow in order)
1. Fetch the starting documentation page (or the memory hint page if provided).
2. Scan for links mentioning "openapi", "swagger", "spec", "yaml", "json", GitHub raw URLs, version numbers, releases, or changelogs.
3. If you see a GitHub repo link, check its releases or tags page to confirm the LATEST version — do NOT assume master/main always has the newest spec.
4. Convert github.com/.../blob/... links to raw.githubusercontent.com/... for direct file access.
5. If multiple versions exist (v2, v3, etc.), always pick the HIGHEST version number.
6. Prefer YAML over JSON when both exist at the same version.
7. List ALL promising candidate URLs you find.

## Efficiency rules
- Fetch at MOST 4 pages total before concluding.
- Do NOT fetch the same URL twice.
- Do NOT fetch login pages, blog posts, or changelogs unless they directly list spec file links.
- If you spot a direct .yaml/.yml/.json spec link immediately, stop and report it.

## Output format — use EXACTLY this block when done:

SPEC_CANDIDATES:
<url1>
<url2>
SEARCH_NOTES: <one sentence about where/how you found it>
SPEC_REPO: <GitHub or source repo URL if applicable, else omit this line>
USEFUL_PAGES: <comma-separated list of pages that were helpful>

If after thorough search you are certain no public spec exists:
NO_SPEC_FOUND

## Common patterns to recognise
- /openapi.yaml, /openapi.json, /swagger.yaml, /swagger.json
- raw.githubusercontent.com/.../openapi.yaml
- GitHub repos named *-openapi, *-oai, *-api-description
- /api/v3/openapi.yaml, /docs/swagger.json
- GitHub Releases page listing versioned spec files
"""

TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetches a web page and returns its text content plus relevant links. "
            "Use this to navigate documentation pages. Do NOT fetch the same URL twice."
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
        """Parse the structured output block from the agent."""
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
    ) -> Optional[Dict[str, Any]]:
        """
        Main agent loop. Always does a fresh search for the latest spec.
        Memory guides navigation strategy, never skips the search.

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

        # ── Load memory for this API ──
        mem_entry = get_memory_entry(starting_url)
        memory_hint = build_memory_hint(mem_entry) if mem_entry else ""
        last_known_version = (mem_entry or {}).get("last_found_version")

        if memory_hint:
            print(f"  [memory] Previous search data found — guiding agent navigation")
            if last_known_version:
                print(f"  [memory] Last known version: {last_known_version}")

        # ── Build system prompt (base + memory hint appended) ──
        system_prompt = BASE_SYSTEM_PROMPT + memory_hint

        # ── Initial user message ──
        user_msg = f"Find the LATEST OpenAPI spec URL starting from: {starting_url}\n"
        if api_name:
            user_msg += f"API name: {api_name}\n"
        if last_known_version:
            user_msg += (
                f"The last known version was {last_known_version}. "
                "Check if a newer version exists — but always return the latest regardless.\n"
            )
        user_msg += (
            "\nFetch at most 4 pages total. "
            "Output the SPEC_CANDIDATES: block as soon as you have any plausible URL."
        )

        messages = [{"role": "user", "content": user_msg}]
        fetched_urls: set = set()
        parsed_output: Optional[Dict[str, Any]] = None

        # ── LLM agent loop ──
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
                        update_memory_entry(starting_url, {
                            "last_search_outcome": "not_found",
                        })
                        return None

            if found_candidates:
                break

            # ── Handle tool calls ──
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
                        "Conclude now. Output SPEC_CANDIDATES: followed by URLs "
                        "and the metadata lines, or output NO_SPEC_FOUND."
                    ),
                })

        # ── Programmatic validation — runs ONCE, outside LLM loop ──
        if not parsed_output or not parsed_output["candidates"]:
            print("  No candidates collected from agent.")
            return None

        candidates = parsed_output["candidates"]
        print(f"\n[validation] Validating {len(candidates)} candidate(s)…")

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

        # ── Update memory with search STRATEGY (not just the URL) ──
        memory_update: Dict[str, Any] = {
            "last_found_version": vr["version"],
            "last_found_url": best_url,        # stored for reference / comparison only
            "last_search_outcome": "found",
        }
        if parsed_output.get("spec_repo"):
            memory_update["spec_repo"] = parsed_output["spec_repo"]
        if parsed_output.get("useful_pages"):
            memory_update["useful_pages"] = parsed_output["useful_pages"]
        if parsed_output.get("search_notes"):
            memory_update["search_notes"] = parsed_output["search_notes"]

        update_memory_entry(starting_url, memory_update)
        print(f"  [memory] Updated search strategy for {_memory_key(starting_url)}")

        return {
            "spec_url": best_url,
            "version": vr["version"],
            "format": vr["format"],
            "title": vr["title"],
            "is_new_version": is_new_version,
        }
