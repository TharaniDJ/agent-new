"""
Batch process multiple API documentation URLs.

check_frequency_days field is stored in memory and printed in reports,
but the actual skip-if-not-due logic is TODO (inactive until tested).
"""

import json
import os
from datetime import datetime, timezone
from openapi_agent import OpenAPIAgent, get_memory_entry, _memory_key

# ─────────────────────────────────────────────────────────────────────────────
# Add your APIs here.
# check_frequency_days: how often you intend to re-check this API for updates.
# Currently informational only — the agent always runs a fresh search.
# ─────────────────────────────────────────────────────────────────────────────
API_DOCS = [
    {
        "name": "Stripe",
        "docs_url": "https://docs.stripe.com/api",
        "check_frequency_days": 1,      # updates frequently
    },
    {
        "name": "GitHub",
        "docs_url": "https://docs.github.com/en/rest",
        "check_frequency_days": 7,      # check weekly
    },
    {
        "name": "OpenAI",
        "docs_url": "https://platform.openai.com/docs/api-reference",
        "check_frequency_days": 7,
    },
    # Add more APIs below:
    # {
    #     "name": "Twilio",
    #     "docs_url": "https://www.twilio.com/docs/usage/api",
    #     "check_frequency_days": 30,   # updates rarely
    # },
]


def should_skip(api: dict) -> bool:
    """
    TODO: Enable this once frequency-skipping logic is tested.

    Intended behaviour:
      - Read last_searched from memory for this API
      - If (now - last_searched) < check_frequency_days → return True (skip)
      - Otherwise → return False (run)

    Currently always returns False so every API is always processed.
    """
    # ── INACTIVE — uncomment block below to enable ──
    # entry = get_memory_entry(api["docs_url"])
    # if entry and entry.get("last_searched"):
    #     last = datetime.fromisoformat(entry["last_searched"])
    #     elapsed = (datetime.now(timezone.utc) - last).days
    #     freq = api.get("check_frequency_days", 1)
    #     if elapsed < freq:
    #         print(f"  [skip] {api['name']}: checked {elapsed}d ago, frequency={freq}d")
    #         return True
    return False


def batch_find_specs(output_file: str = "openapi_specs.json"):
    """Find and validate the latest OpenAPI specs for all configured APIs."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY environment variable")

    agent = OpenAPIAgent(api_key)
    results = []

    for api in API_DOCS:
        print(f"\n{'='*70}")
        print(f"Processing  : {api['name']}")
        print(f"Docs URL    : {api['docs_url']}")
        print(f"Frequency   : every {api.get('check_frequency_days', '?')} day(s)  [frequency-skip is inactive]")
        print("=" * 70)

        if should_skip(api):
            # Only reached once TODO block is activated
            results.append({
                "name": api["name"],
                "docs_url": api["docs_url"],
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
        )

        if result_data:
            result = {
                "name": api["name"],
                "docs_url": api["docs_url"],
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
            version_tag = f"  (NEW VERSION: {result_data['version']})" if result_data["is_new_version"] else f"  v{result_data['version']}"
        else:
            result = {
                "name": api["name"],
                "docs_url": api["docs_url"],
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

    # ── Save results ──
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
            tag = " ← NEW VERSION" if r["is_new_version"] else ""
            print(f"  ✓ {r['name']:20s} {r['format'].upper() if r['format'] else '?':5s}  v{r['version']}  {r['spec_url']}{tag}")
        elif r["status"] == "skipped":
            print(f"  - {r['name']:20s} skipped (frequency not due)")
        else:
            print(f"  ✗ {r['name']:20s} not found")


if __name__ == "__main__":
    batch_find_specs()
