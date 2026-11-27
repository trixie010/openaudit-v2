#!/usr/bin/env python3
"""Corpus retrieval probe for an eval run (run AFTER blind candidates exist).

This is a prep/diagnostic, not a substitute for a blind replay. It measures:

- retrievability: for each answer-key H/M, do top-k corpus hints exist?
- leakage: how many top-k hits come from the same contest when exclusions are
  disabled. The excluded run must show zero leakage or the A/B is contaminated.

It does NOT decide whether a hint is genuinely relevant; read the generated MD
and score that yourself (or feed it to the judge later).

Typical use, after `evalctl.py parse/score`:

  python scripts/corpus_probe.py 2024-06-vultisig \\
      --workdir ~/openaudit/eval-runs/2024-06-vultisig

  # leakage baseline (same contest rows intentionally left in):
  python scripts/corpus_probe.py 2024-06-vultisig \\
      --workdir ~/openaudit/eval-runs/2024-06-vultisig --no-meta-excludes

The answer key is read here by design - this runs at confirm time, never during
the blind discovery pass.
"""

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_ROOT / "disclosed-findings-corpus" / "scripts"))

from retrieval.index import FindingsIndex  # noqa: E402

DEFAULT_DB = SKILL_ROOT / "disclosed-findings-corpus" / "data" / "findings.db"


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _same_contest(hit, slug: str, title: str) -> bool:
    url = (hit.source_url or "").lower()
    pname = (hit.protocol_name or "").lower()
    return bool(
        (slug and slug.lower() in url)
        or (title and title.lower() in pname)
    )


def _hit(hit, slug: str, title: str) -> dict:
    return {
        "title": hit.title,
        "severity": hit.severity,
        "source": hit.source,
        "protocol_name": hit.protocol_name,
        "bug_class": hit.bug_class,
        "source_url": hit.source_url,
        "same_contest": _same_contest(hit, slug, title),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe corpus retrievability/leakage for an eval run.")
    ap.add_argument("slug", help="eval-run slug, e.g. 2024-06-vultisig")
    ap.add_argument("--workdir", default=None, help="default: eval-runs/<slug>")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="corpus DB")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--body-chars", type=int, default=0,
                    help="GT body chars appended to the query (title is always used; "
                         "0 = title-only, best signal/speed; long bodies add common-word noise)")
    ap.add_argument("--exclude-protocol", action="append", default=[], metavar="SUBSTRING",
                    help="extra protocol-name exclusion (repeatable)")
    ap.add_argument("--exclude-url", action="append", default=[], metavar="SUBSTRING",
                    help="extra source-url exclusion (repeatable)")
    ap.add_argument("--no-meta-excludes", action="store_true",
                    help="disable the automatic target title/slug exclusions (leakage baseline)")
    ap.add_argument("--no-leakage", action="store_true", help="skip the un-excluded baseline")
    ap.add_argument("--out", default=None, help="output prefix (default: <workdir>/corpus_probe)")
    args = ap.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path("eval-runs") / args.slug
    keydir = workdir / "_answer_key"
    meta_path = keydir / "meta.json"
    gt_path = keydir / "ground_truth.jsonl"
    if not meta_path.exists() or not gt_path.exists():
        print(f"missing answer key under {keydir}", file=sys.stderr)
        return 1
    db = Path(args.db)
    if not db.exists():
        print(f"missing corpus DB: {db}", file=sys.stderr)
        return 1

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    slug = str(meta.get("slug") or args.slug)
    title = str(meta.get("title") or "")
    ex_protocol = list(args.exclude_protocol)
    ex_url = list(args.exclude_url)
    if not args.no_meta_excludes:
        if title:
            ex_protocol.append(title)
        ex_url.append(slug)

    idx = FindingsIndex(db, exclude_protocol=ex_protocol, exclude_url=ex_url)
    raw_idx = None if args.no_leakage else FindingsIndex(db)

    rows = []
    for gt in _load_jsonl(gt_path):
        gt_title = str(gt.get("title") or "")
        query = f"{gt_title}\n{str(gt.get('body') or '')[:args.body_chars]}"
        hits = idx.hint_search(query, top_k=args.top)
        raw_hits = raw_idx.hint_search(query, top_k=args.top) if raw_idx else []
        row = {
            "id": gt.get("id"),
            "severity": gt.get("severity"),
            "title": gt_title,
            "hits": [_hit(h, slug, title) for h in hits],
            "leakage_hits": [
                _hit(h, slug, title) for h in raw_hits
                if _same_contest(h, slug, title)
            ],
        }
        rows.append(row)

    with_hits = sum(1 for row in rows if row["hits"])
    excluded_same = sum(
        1 for row in rows for h in row["hits"] if h["same_contest"]
    )
    leakage_total = sum(len(row["leakage_hits"]) for row in rows)
    payload = {
        "slug": slug,
        "title": title,
        "db": str(db),
        "top_k": args.top,
        "exclude_protocol": ex_protocol,
        "exclude_url": ex_url,
        "gt_total": len(rows),
        "gt_with_hits": with_hits,
        "same_contest_hits_with_exclusions": excluded_same,
        "leakage_hits_without_exclusions": leakage_total,
        "rows": rows,
    }

    base = Path(args.out) if args.out else workdir / "corpus_probe"
    base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    md = [
        f"# Corpus probe - {slug}",
        "",
        f"- GT H/M: {len(rows)}; GT with at least one hint: {with_hits}",
        f"- Top-k: {args.top}",
        f"- Excluded protocol substrings: {', '.join(ex_protocol) or '(none)'}",
        f"- Excluded URL substrings: {', '.join(ex_url) or '(none)'}",
        f"- Same-contest hits in the excluded run: {excluded_same}",
        f"- Same-contest hits without exclusions: {leakage_total}",
        "",
        "| GT | Sev | Top corpus hints |",
        "|---|---|---|",
    ]
    for row in rows:
        if row["hits"]:
            desc = "<br>".join(
                f"{h['severity']} {h['source']} | {h['title']} | {h['source_url']}"
                for h in row["hits"][:3]
            )
        else:
            desc = "_no hits_"
        md.append(f"| {row['id']}: {row['title']} | {row['severity']} | {desc} |")

    if raw_idx is not None:
        md += [
            "",
            "## Leakage baseline",
            "",
            f"Without target exclusions, {leakage_total} top-k hit(s) came from the same "
            "contest. This should be >0 when the corpus contains the target; the excluded "
            "probe above must be 0.",
        ]

    base.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if excluded_same and not args.no_meta_excludes:
        print("WARNING: exclusions did not remove every same-contest hit", file=sys.stderr)
    print(
        f"gt={len(rows)} with_hits={with_hits} "
        f"same_contest_with_exclusions={excluded_same} "
        f"leakage_without_exclusions={leakage_total} "
        f"-> {base.with_suffix('.md')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
