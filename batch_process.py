"""
Batch process multiple API documentation URLs.

Supports multi-spec entries: one docs URL can produce multiple separate spec results
(e.g. Candid which has CharityCheckPdf, Essentials, Premier as separate specs).

check_frequency_days is stored but the skip logic is inactive until tested (see TODO).

API notes:
- Guidewire InsNow: portal requires auth; agent will attempt but may not find a public spec
- HubSpot: specs are split per product in github.com/HubSpot/HubSpot-public-api-spec-collection
  Each HubSpot module needs its own entry with target_spec pointing to the product folder name
- Google APIs: specs served via apis.guru discovery layer (googleapis.com entries)
"""

import json
import os
from datetime import datetime, timezone
from openapi_agent import OpenAPIAgent, get_memory_entry

API_DOCS = [
    # ── Guidewire ─────────────────────────────────────────────────────────────
    # ⚠ InsNow API access requires a Guidewire partner/customer account.
    # The public API reference page exists but the spec download may be gated.
    # Agent will attempt; expect not_found if the portal blocks bots.
    {
        "name": "Guidewire InsNow",
        "docs_url": "https://www.guidewire.com/Developers/APIs/InsuranceNow-APIs",
        "check_frequency_days": 30,
    },

    # ── HubSpot ───────────────────────────────────────────────────────────────
    # HubSpot hosts all their per-product OpenAPI specs in one GitHub repo:
    # github.com/HubSpot/HubSpot-public-api-spec-collection
    # The folder structure is: PublicApiSpecs/{Category}/{ProductName}/
    # Each entry below uses the same docs_url (the GitHub repo) and a
    # target_spec that tells the agent which product folder to look in.
    {
        "name": "HubSpot Automation Actions",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Automation_Actions",
    },
    {
        "name": "HubSpot CRM Associations",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Associations",
    },
    {
        "name": "HubSpot CRM Associations Schema",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Association_Schema",
    },
    {
        "name": "HubSpot CRM Commerce Carts",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Carts",
    },
    {
        "name": "HubSpot CRM Commerce Discounts",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Discounts",
    },
    {
        "name": "HubSpot CRM Commerce Orders",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Orders",
    },
    {
        "name": "HubSpot CRM Commerce Quotes",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Quotes",
    },
    {
        "name": "HubSpot CRM Commerce Taxes",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Taxes",
    },
    {
        "name": "HubSpot CRM Engagement Meeting",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Meetings",
    },
    {
        "name": "HubSpot CRM Engagement Notes",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Notes",
    },
    {
        "name": "HubSpot CRM Engagements Calls",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Calls",
    },
    {
        "name": "HubSpot CRM Engagements Communications",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Communications",
    },
    {
        "name": "HubSpot CRM Engagements Email",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Emails",
    },
    {
        "name": "HubSpot CRM Engagements Tasks",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Tasks",
    },
    {
        "name": "HubSpot CRM Extensions Timelines",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Timeline",
    },
    {
        "name": "HubSpot CRM Extensions Videoconferencing",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Video_Conferencing_Extension",
    },
    {
        "name": "HubSpot CRM Import",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Imports",
    },
    {
        "name": "HubSpot CRM Lists",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Lists",
    },
    {
        "name": "HubSpot CRM Object Companies",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Companies",
    },
    {
        "name": "HubSpot CRM Object Contacts",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Contacts",
    },
    {
        "name": "HubSpot CRM Object Deals",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Deals",
    },
    {
        "name": "HubSpot CRM Object Feedback",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Feedback_Submissions",
    },
    {
        "name": "HubSpot CRM Object Leads",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Leads",
    },
    {
        "name": "HubSpot CRM Object Line Items",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Line_Items",
    },
    {
        "name": "HubSpot CRM Object Products",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Products",
    },
    {
        "name": "HubSpot CRM Object Schemas",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Schemas",
    },
    {
        "name": "HubSpot CRM Object Tickets",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Tickets",
    },
    {
        "name": "HubSpot CRM Owners",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Owners",
    },
    {
        "name": "HubSpot CRM Pipelines",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Pipelines",
    },
    {
        "name": "HubSpot CRM Properties",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Properties",
    },
    {
        "name": "HubSpot Marketing Campaigns",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Campaigns",
    },
    {
        "name": "HubSpot Marketing Emails",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Marketing_Emails",
    },
    {
        "name": "HubSpot Marketing Events",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Marketing_Events",
    },
    {
        "name": "HubSpot Marketing Forms",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Forms",
    },
    {
        "name": "HubSpot Marketing Subscriptions",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Subscription_Preferences",
    },
    {
        "name": "HubSpot Marketing Transactional",
        "docs_url": "https://github.com/HubSpot/HubSpot-public-api-spec-collection",
        "check_frequency_days": 7,
        "target_spec": "Transactional_Emails",
    },

    # ── Google APIs ───────────────────────────────────────────────────────────
    # Google does not publish native OpenAPI 3.x specs for its Workspace APIs.
    # The community-maintained apis.guru project converts Google's Discovery
    # Documents into OpenAPI 3.x and is the most widely used source.
    # Docs URL used is the official Google developer reference for each API.
    {
        "name": "Google Calendar API",
        "docs_url": "https://developers.google.com/workspace/calendar/api/v3/reference",
        "check_frequency_days": 30,
    },
    {
        "name": "Gmail API",
        "docs_url": "https://developers.google.com/workspace/gmail/api/reference/rest",
        "check_frequency_days": 30,
    },
]


def should_skip(api: dict) -> bool:
    """
    TODO: Enable frequency-skipping once tested.
    Currently always returns False — every API always runs a fresh search.

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
            print(f"  ✓ {r['name']:45s} {fmt:5s}  v{r['version']}  {r['spec_url']}{tag}")
        elif r["status"] == "skipped":
            print(f"  - {r['name']:45s} skipped")
        else:
            print(f"  ✗ {r['name']:45s} not found")


if __name__ == "__main__":
    batch_find_specs()
