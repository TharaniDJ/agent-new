"""
Batch process multiple API documentation URLs.

Supports multi-spec entries: one docs URL can produce multiple separate spec results
(e.g. Candid which has CharityCheckPdf, Essentials, Premier as separate specs).

check_frequency_days is stored but the skip logic is inactive until tested (see TODO).
"""

import json
import os
from datetime import datetime, timezone
from openapi_agent import OpenAPIAgent, get_memory_entry

# ─────────────────────────────────────────────────────────────────────────────
# API list.
#
# For multi-spec APIs, add "target_specs" — a list of named specs to extract
# separately from the same docs URL.
# Each entry in target_specs becomes its own row in the output.
# ─────────────────────────────────────────────────────────────────────────────
API_DOCS = [
    # ── Previously failing / mentioned in review ──────────────────────────────
    {
        "name": "Asana",
        "docs_url": "https://developers.asana.com/reference/rest-api-reference",
        "check_frequency_days": 7,
    },
    {
        "name": "GitHub",
        "docs_url": "https://docs.github.com/en/rest",
        "check_frequency_days": 7,
    },
    {
        "name": "DocuSign Admin API",
        "docs_url": "https://developers.docusign.com/docs/admin-api/",
        "check_frequency_days": 30,
    },
    {
        "name": "DocuSign Click API",
        "docs_url": "https://developers.docusign.com/docs/click-api/",
        "check_frequency_days": 30,
    },
    {
        "name": "DocuSign eSign API",
        "docs_url": "https://developers.docusign.com/docs/esign-rest-api/",
        "check_frequency_days": 30,
    },

    # ── Candid: 3 specific specs from one docs URL ────────────────────────────
    {
        "name": "Candid CharityCheckPdf",
        "docs_url": "https://developer.candid.org/reference/openapi",
        "check_frequency_days": 30,
        "target_spec": "CharityCheckPdf",    # tells agent which named spec to find
    },
    {
        "name": "Candid Essentials",
        "docs_url": "https://developer.candid.org/reference/openapi",
        "check_frequency_days": 30,
        "target_spec": "Essentials",
    },
    {
        "name": "Candid Premier",
        "docs_url": "https://developer.candid.org/reference/openapi",
        "check_frequency_days": 30,
        "target_spec": "Premier API",
    },

    # ── New APIs from ballerinax modules ──────────────────────────────────────
    {
        "name": "Discord",
        "docs_url": "https://discord.com/developers/docs/reference",
        "check_frequency_days": 7,
    },
    {
        "name": "Dayforce",
        "docs_url": "https://developers.dayforce.com/Build/Home.aspx",
        "check_frequency_days": 30,
    },
]


def should_skip(api: dict) -> bool:
    """
    TODO: Enable frequency-skipping once tested.
    Currently always returns False — every API always runs.

    Uncomment the block below to activate:
    """
    # entry = get_memory_entry(api["docs_url"])
    # if entry and entry.get("last_searched"):
    #     from datetime import datetime, timezone
    #     last = datetime.fromisoformat(entry["last_searched"])
    #     elapsed = (datetime.now(timezone.utc) - last).days
    #     freq = api.get("check_frequency_days", 1)
    #     if elapsed < freq:
    #         print(f"  [skip] {api['name']}: checked {elapsed}d ago, frequency={freq}d")
    #         return True
    return False


def batch_find_specs(output_file: str = "openapi_specs.json"):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY environment variable")

    agent = OpenAPIAgent(api_key)
    results = []

    for api in API_DOCS:
        print(f"\n{'='*70}")
        print(f"Processing  : {api['name']}")
        print(f"Docs URL    : {api['docs_url']}")
        if api.get("target_spec"):
            print(f"Target spec : {api['target_spec']}")
        print(f"Frequency   : every {api.get('check_frequency_days', '?')} day(s)  [skip inactive]")
        print("=" * 70)

        if should_skip(api):
            results.append({
                "name": api["name"],
                "docs_url": api["docs_url"],
                "target_spec": api.get("target_spec"),
                "spec_url": None,
                "version": None,
                "format": None,
                "status": "skipped",
                "is_new_version": False,
                "check_frequency_days": api.get("check_frequency_days"),
            })
            continue

        result_data = agent.run(
            api["docs_url"],
            api_name=api["name"],
            max_iterations=6,
            target_title=api.get("target_spec"),
        )

        if result_data:
            result = {
                "name": api["name"],
                "docs_url": api["docs_url"],
                "target_spec": api.get("target_spec"),
                "spec_url": result_data["spec_url"],
                "version": result_data["version"],
                "format": result_data["format"],
                "title": result_data.get("title"),
                "status": "found",
                "is_new_version": result_data["is_new_version"],
                "check_frequency_days": api.get("check_frequency_days"),
                "found_at": datetime.now(timezone.utc).isoformat(),
            }
            symbol = "✓"
            version_tag = (
                f"  (NEW VERSION: {result_data['version']})"
                if result_data["is_new_version"]
                else f"  v{result_data['version']}"
            )
        else:
            result = {
                "name": api["name"],
                "docs_url": api["docs_url"],
                "target_spec": api.get("target_spec"),
                "spec_url": None,
                "version": None,
                "format": None,
                "title": None,
                "status": "not_found",
                "is_new_version": False,
                "check_frequency_days": api.get("check_frequency_days"),
                "found_at": None,
            }
            symbol = "✗"
            version_tag = ""

        results.append(result)
        spec_display = result["spec_url"] or "Not found"
        print(f"\n{symbol} {api['name']}: {spec_display}{version_tag}")

    # ── Save ──
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"Results saved to : {output_file}")
    print("=" * 70)

    found   = sum(1 for r in results if r["status"] == "found")
    new_ver = sum(1 for r in results if r["is_new_version"])
    skipped = sum(1 for r in results if r["status"] == "skipped")

    print(f"\nSummary: {found}/{len(results)} found  |  {new_ver} new version(s)  |  {skipped} skipped\n")

    for r in results:
        if r["status"] == "found":
            tag  = " ← NEW VERSION" if r["is_new_version"] else ""
            fmt  = (r["format"] or "?").upper()
            name = r["name"]
            print(f"  ✓ {name:30s} {fmt:5s}  v{r['version']}  {r['spec_url']}{tag}")
        elif r["status"] == "skipped":
            print(f"  - {r['name']:30s} skipped")
        else:
            print(f"  ✗ {r['name']:30s} not found")


if __name__ == "__main__":
    batch_find_specs()
