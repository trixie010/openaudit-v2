"""
Ingest disclosed audit findings (Solodit exports, C4/Sherlock/Spearbit
writeups you've pulled down yourself) into a structured SQLite table.

Input: a .jsonl file, one finding per line, with this schema:

{
  "source": "code4rena" | "sherlock" | "spearbit" | "solodit" | ...,
  "protocol_type": "lending" | "amm" | "vault" | "bridge" | "staking" | ...,
  "bug_class": "oracle-manipulation" | "reentrancy" | "share-inflation" | ...,
  "title": "...",
  "root_cause": "plain-English root cause",
  "pattern": "the code SHAPE to grep/semantically-match for, not a quote",
  "preconditions": "what has to be true for this to be exploitable",
  "severity": "critical" | "high" | "medium" | "low",
  "fix": "the standard fix",
  "protocol_name": "optional - the actual protocol this was found in, used only for
                     dedup_search's exact-match scope, not for hint_search"
}

Usage:
    python ingest/ingest_findings.py data/sample_findings.jsonl [more.jsonl ...]

Note on copyright/ToS: scrape/export findings yourself from sources whose
terms allow it (Solodit, C4 and Sherlock repos are public; some private
audit firm PDFs are not meant for redistribution). This script only
structures data you already have - it doesn't fetch anything itself.
"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "findings.db"  # scripts/ingest/ -> skill root / data/

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    protocol_type TEXT,
    bug_class TEXT,
    title TEXT,
    root_cause TEXT,
    pattern TEXT,
    preconditions TEXT,
    severity TEXT,
    fix TEXT,
    protocol_name TEXT,
    poc_source TEXT,
    raw_json TEXT
);
"""


def ingest(paths: list[str]) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    count = 0
    skipped = 0
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"  skip {path}:{line_no} - bad json ({e})")
                    continue
                # Append-only DB, but re-ingesting the same file must not duplicate.
                # Identity = source + title + root_cause (stable across re-runs).
                exists = conn.execute(
                    "SELECT 1 FROM findings WHERE source IS ? AND title IS ? AND root_cause IS ? LIMIT 1",
                    (rec.get("source"), rec.get("title"), rec.get("root_cause")),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO findings
                       (source, protocol_type, bug_class, title, root_cause,
                        pattern, preconditions, severity, fix, protocol_name, poc_source, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rec.get("source"),
                        rec.get("protocol_type"),
                        rec.get("bug_class"),
                        rec.get("title"),
                        rec.get("root_cause"),
                        rec.get("pattern"),
                        rec.get("preconditions"),
                        rec.get("severity"),
                        rec.get("fix"),
                        rec.get("protocol_name", ""),
                        rec.get("poc_source", ""),
                        json.dumps(rec),
                    ),
                )
                count += 1
    conn.commit()
    conn.close()
    if skipped:
        print(f"  skipped {skipped} duplicate row(s) already present")
    return count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python ingest_findings.py file1.jsonl [file2.jsonl ...]")
        sys.exit(1)
    n = ingest(sys.argv[1:])
    print(f"Ingested {n} findings into {DB_PATH}")
