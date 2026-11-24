"""
Solodit ingester - the real documented API, not a scraper.

Endpoint: POST https://solodit.cyfrin.io/api/v1/solodit/findings
Header:   X-Cyfrin-API-Key: <your key>
Rate limit: 20 requests / 60s (documented) - this sleeps between pages to respect it.

Get a key: create an account at solodit.cyfrin.io, profile dropdown -> API Keys.

I could NOT test this against the live API in the sandbox this was built in -
solodit.cyfrin.io isn't reachable from there (network allowlist), and I don't
have a key either way. The request side (URL, header, body shape, filters,
pagination, rate limiting) is built exactly from Cyfrin's own documented spec
and a published API-client skill that quotes real example requests - that
part should be right. The RESPONSE side - the exact field names on each
finding object - isn't documented anywhere I could find, so `_map_finding()`
below is defensive: it tries several plausible field name variants for each
of our schema's fields, and logs the raw keys of the first result so you can
see immediately whether the mapping needs adjusting on your first real run.

Usage:
    export CYFRIN_API_KEY="sk_..."
    python ingest/sources/solodit.py --impact HIGH MEDIUM --protocol-categories Lending DeFi \
        --max-pages 10 --out data/solodit_findings.jsonl
    python ingest/ingest_findings.py data/solodit_findings.jsonl
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests

API_URL = "https://solodit.cyfrin.io/api/v1/solodit/findings"
RATE_LIMIT_SLEEP_SECONDS = 3.1  # 20 req/60s = one every 3s minimum; small buffer added


def _first(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _map_finding(raw: dict) -> dict:
    """Defensive field mapping - see module docstring for why this guesses across
    several plausible key names instead of assuming one documented shape."""
    severity = _first(raw, "impact", "severity", "Impact", "Severity").lower()
    tags = _first(raw, "tags", "Tags", default=[])
    if isinstance(tags, list):
        bug_class = tags[0] if tags else ""
    else:
        bug_class = str(tags)

    return {
        "source": "solodit:" + _first(raw, "firm", "audit_firm", "source", "Firm", default="unknown"),
        "protocol_type": _first(raw, "protocol_category", "category", "ProtocolCategory").lower(),
        "bug_class": bug_class,
        "title": _first(raw, "title", "Title", "name"),
        "root_cause": _first(raw, "description", "content", "Description", "summary"),
        "pattern": _first(raw, "summary", "Summary", "description", "content"),
        "preconditions": "",  # Solodit findings are narrative audit text, not structured
                               # preconditions - left blank rather than guessing from prose.
        "severity": severity,
        "fix": _first(raw, "recommendation", "fix", "Recommendation", "remediation"),
        "protocol_name": _first(raw, "protocol", "protocol_name", "Protocol"),
    }


def fetch_page(api_key: str, page: int, page_size: int, filters: dict) -> dict:
    resp = requests.post(
        API_URL,
        headers={"Content-Type": "application/json", "X-Cyfrin-API-Key": api_key},
        json={"page": page, "pageSize": page_size, "filters": filters},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def pull(
    api_key: str,
    impact: list[str] | None = None,
    protocol_categories: list[str] | None = None,
    tags: list[str] | None = None,
    keywords: str = "",
    max_pages: int = 20,
    page_size: int = 100,
) -> list[dict]:
    filters = {}
    if impact:
        filters["impact"] = impact
    if protocol_categories:
        filters["protocol_categories"] = protocol_categories
    if tags:
        filters["tags"] = tags
    if keywords:
        filters["keywords"] = keywords

    results = []
    for page in range(1, max_pages + 1):
        data = fetch_page(api_key, page, page_size, filters)
        # Response envelope shape is also undocumented beyond "a list of findings somewhere" -
        # try the common envelope keys, fall back to treating the whole response as the list.
        items = data.get("results") or data.get("findings") or data.get("data") or data
        if isinstance(items, dict):
            items = items.get("items", [])
        if not items:
            print(f"  [solodit] page {page}: no more results, stopping")
            break

        if page == 1 and items:
            print(f"  [solodit] first result's raw keys (for sanity-checking the field "
                  f"mapping): {list(items[0].keys())}")

        results.extend(_map_finding(item) for item in items)
        print(f"  [solodit] page {page}: {len(items)} findings (running total: {len(results)})")

        if len(items) < page_size:
            break  # last page
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impact", nargs="*", default=None, help="e.g. HIGH MEDIUM")
    ap.add_argument("--protocol-categories", nargs="*", default=None, help="e.g. Lending DeFi Bridge")
    ap.add_argument("--tags", nargs="*", default=None, help="e.g. Reentrancy Oracle")
    ap.add_argument("--keywords", default="")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--out", default="data/solodit_findings.jsonl")
    args = ap.parse_args()

    api_key = os.environ.get("CYFRIN_API_KEY")
    if not api_key:
        raise SystemExit("Set CYFRIN_API_KEY first (create an account at solodit.cyfrin.io -> "
                          "profile dropdown -> API Keys).")

    results = pull(
        api_key,
        impact=args.impact,
        protocol_categories=args.protocol_categories,
        tags=args.tags,
        keywords=args.keywords,
        max_pages=args.max_pages,
        page_size=args.page_size,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for rec in results:
            f.write(json.dumps(rec) + "\n")
    print(f"Wrote {len(results)} findings to {out_path}")
    print(f"Next: python ingest/ingest_findings.py {out_path}")


if __name__ == "__main__":
    main()
