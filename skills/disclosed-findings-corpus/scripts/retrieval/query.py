#!/usr/bin/env python3
"""CLI for the disclosed-findings corpus.

Hint search (discovery, one Category B pipeline's angle - pattern grounding):
  python scripts/retrieval/query.py "first depositor share inflation vault" --protocol-type vault --top 3

Dedup search (confirm-time ONLY, protocol-name substring scope):
  python scripts/retrieval/query.py "cross-chain message replay" --dedup --protocol-name MyProto --top 5

Blind-eval guard (repeatable): drop the target's own corpus rows so an A/B replay is not
contaminated:
  python scripts/retrieval/query.py "share inflation" --exclude-protocol Vultisig \
      --exclude-url 2024-06-vultisig

Zero third-party deps: retrieval is SQLite FTS5 from the stdlib.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ on path -> `retrieval.*`

from retrieval.index import FindingsIndex  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Query the disclosed-findings corpus.")
    ap.add_argument("query", help="free-text description of the code behaviour / bug shape")
    ap.add_argument("--protocol-type", default=None, help="hint filter: lending|amm|vault|staking|bridge|...")
    ap.add_argument("--protocol-name", default=None, help="dedup filter: target protocol name (substring; e.g. Tornado hits Tornado.Cash)")
    ap.add_argument("--dedup", action="store_true", help="use dedup_search (confirm-time) instead of hint_search")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--db", default=None, help="path to findings.db (default: skill data/findings.db)")
    ap.add_argument("--exclude-protocol", action="append", default=[], metavar="SUBSTRING",
                    help="drop rows whose protocol_name contains this (repeatable; for blind eval)")
    ap.add_argument("--exclude-url", action="append", default=[], metavar="SUBSTRING",
                    help="drop rows whose source_url contains this (repeatable; for blind eval)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None
    idx = FindingsIndex(
        db_path,
        exclude_protocol=args.exclude_protocol,
        exclude_url=args.exclude_url,
    )
    if args.dedup:
        hits = idx.dedup_search(args.query, protocol_name=args.protocol_name, top_k=args.top)
        for h in hits:
            print(h.as_dedup_record())
            print("---")
    else:
        hits = idx.hint_search(args.query, protocol_type=args.protocol_type, top_k=args.top)
        for h in hits:
            print(h.as_hint())
            print("---")
    if not hits:
        print("(no matches — corpus may be empty, or the query is too narrow)")


if __name__ == "__main__":
    main()
