"""
Rekt.news ingester - post-mortem narrative, not structured audit findings.
Extracting the technical root cause from that narrative into our schema is
the CALLING AGENT's own reasoning step (see this skill's SKILL.md), not a
second LLM call made from inside this script - this script only fetches and
stores raw article text (--fetch-articles) for the agent to read afterward.

HONESTY NOTE: I confirmed the leaderboard's actual CONTENT is real - fetched
https://rekt.news/leaderboard and saw all ~331 entries (title, embedded
auditor note, $ lost, date, article slug) in one non-paginated page. What I
could NOT verify is the raw HTML structure: I only ever saw that page through
a markdown-rendering fetch tool, not the actual DOM a `requests`+`bs4` scraper
sees. The CSS selectors below are my best inference from the rendered content,
not a verified match against real HTML tags/classes. rekt.news also isn't
reachable from the sandbox this was built in (DNS resolution fails under its
network allowlist), so none of this module's network calls were tested live.
Run `python ingest/sources/rekt.py --limit 3` yourself first and inspect the
output before trusting it on the full leaderboard - if the selectors are
wrong, `parse_leaderboard()` will likely return zero or garbage entries, which
should be obvious immediately rather than silently wrong.

Usage:
    python ingest/sources/rekt.py --limit 20 --fetch-articles --out data/rekt_findings.jsonl
    # then the calling agent reads _article_text per record and fills in
    # bug_class/protocol_type/root_cause/pattern itself before ingest_findings.py
"""

import argparse
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LEADERBOARD_URL = "https://rekt.news/leaderboard"
NAV_LABELS = {"t&c", "videos", "leaderboard", "research", "log in", "subscribe", ""}


def parse_leaderboard(html: str) -> list[dict]:
    """Best-effort inferred selectors - see module docstring. Expects each entry to be
    reachable as an <a> tag linking to a rekt.news article, with a dollar amount and a
    date somewhere in the same list item / nearby text."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    seen_slugs = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if text.lower() in NAV_LABELS:
            continue
        # Article links are relative ("/slug") or absolute ("https://rekt.news/slug"),
        # single path segment, excluding known non-article paths.
        m = re.match(r"^(?:https?://(?:www\.)?rekt\.news)?/([\w-]+)/?$", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen_slugs or slug in ("leaderboard", "research", "termAndConditions"):
            continue

        # Look for a dollar amount and a date in the containing block (parent li/div),
        # since the amount/date are rendered as sibling text near the link, not inside it.
        container = a.find_parent(["li", "div"]) or a.parent
        context_text = container.get_text(" ", strip=True) if container else ""
        amount_m = re.search(r"\$[\d,]+", context_text)
        date_m = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", context_text)

        entries.append({
            "title": text,
            "slug": slug,
            "url": f"https://rekt.news/{slug}",
            "amount_lost": amount_m.group(0) if amount_m else "",
            "date": date_m.group(0) if date_m else "",
        })
        seen_slugs.add(slug)

    return entries


def fetch_leaderboard() -> list[dict]:
    resp = requests.get(LEADERBOARD_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return parse_leaderboard(resp.text)


def fetch_article_text(url: str) -> str:
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Strip nav/footer/script noise, keep the article body's text.
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def build_record(entry: dict, fetch_article: bool) -> dict:
    """bug_class/protocol_type/root_cause/pattern are left EMPTY here - this script only
    fetches raw article text (narrative, not structured). Extracting the technical root
    cause from that narrative is the calling agent's own reasoning step (see SKILL.md's
    "Classify a batch" instruction), not a second LLM call from inside this script."""
    rec = {
        "source": "rekt.news",
        "protocol_type": "",
        "bug_class": "",
        "title": entry["title"],
        "root_cause": "",
        "pattern": "",
        "preconditions": "",
        "severity": "",
        "fix": "",
        "protocol_name": entry["title"].split(" - ")[0].strip(),
        "poc_source": "",  # rekt.news has no PoC code, only narrative
        "_rekt_meta": {"url": entry["url"], "amount_lost": entry["amount_lost"], "date": entry["date"]},
    }

    if fetch_article:
        try:
            rec["_article_text"] = fetch_article_text(entry["url"])[:8000]
        except requests.RequestException as e:
            rec["_article_text"] = ""
            rec["_rekt_meta"]["fetch_error"] = str(e)

    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="rekt.news has ~330 entries total - start small")
    ap.add_argument("--fetch-articles", action="store_true",
                     help="fetch each article's raw text into _article_text for the calling agent "
                          "to classify afterward - without this, only title/amount/date/url are kept")
    ap.add_argument("--rate-limit-seconds", type=float, default=2.0,
                     help="pause between article fetches - be a reasonable guest on their server")
    ap.add_argument("--out", default="data/rekt_findings.jsonl")
    args = ap.parse_args()

    print("Fetching leaderboard...")
    entries = fetch_leaderboard()
    print(f"Parsed {len(entries)} leaderboard entries "
          f"(if this is 0 or looks wrong, the inferred selectors need fixing - see module docstring)")
    entries = entries[: args.limit]

    records = []
    for i, entry in enumerate(entries):
        print(f"  [{i+1}/{len(entries)}] {entry['title']}")
        records.append(build_record(entry, args.fetch_articles))
        if args.fetch_articles and i < len(entries) - 1:
            time.sleep(args.rate_limit_seconds)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    print(f"Wrote {len(records)} records to {out_path}")
    print(f"Next: python ingest/ingest_findings.py {out_path}")


if __name__ == "__main__":
    main()
