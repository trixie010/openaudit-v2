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
                     dedup_search's protocol-name substring scope, not for hint_search"
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
    body_text TEXT,
    source_url TEXT,
    source_id TEXT,
    raw_json TEXT
);
"""


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS hint_fts")
    conn.execute("DROP TABLE IF EXISTS dedup_fts")
    conn.execute("CREATE VIRTUAL TABLE hint_fts USING fts5(body, protocol_type UNINDEXED)")
    conn.execute("CREATE VIRTUAL TABLE dedup_fts USING fts5(body, protocol_name UNINDEXED)")
    rows = conn.execute(
        """SELECT id, protocol_type, bug_class, title, root_cause, pattern,
                  preconditions, protocol_name, body_text
           FROM findings"""
    ).fetchall()
    hint_rows = []
    dedup_rows = []
    for rid, ptype, bclass, title, root, pattern, pre, pname, body in rows:
        hint_rows.append((
            rid,
            f"{bclass or ''} {ptype or ''} {title or ''} {root or ''} "
            f"{pattern or ''} {pre or ''} {(body or '')[:6000]}",
            ptype or "",
        ))
        dedup_rows.append((
            rid,
            f"{title or ''} {pname or ''} {root or ''} {(body or '')[:4000]}",
            pname or "",
        ))
    conn.executemany("INSERT INTO hint_fts(rowid, body, protocol_type) VALUES (?,?,?)", hint_rows)
    conn.executemany("INSERT INTO dedup_fts(rowid, body, protocol_name) VALUES (?,?,?)", dedup_rows)
    conn.commit()


def ingest(paths: list[str], db_path: Path = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(findings)").fetchall()}
    for col in ("body_text", "source_url", "source_id"):
        if col not in cols:
            conn.execute(f"ALTER TABLE findings ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_source_id ON findings(source_id)")
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
                # Prefer the source's stable id; fall back to source + title + root_cause.
                source_id = rec.get("source_id") or None
                if source_id:
                    exists = conn.execute(
                        "SELECT 1 FROM findings WHERE source_id IS ? LIMIT 1", (source_id,)
                    ).fetchone()
                else:
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
                        pattern, preconditions, severity, fix, protocol_name, poc_source,
                        body_text, source_url, source_id, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        rec.get("body_text", ""),
                        rec.get("source_url", ""),
                        source_id,
                        json.dumps({k: v for k, v in rec.items() if k != "body_text"}),
                    ),
                )
                count += 1
    _rebuild_fts(conn)
    conn.commit()
    conn.close()
    if skipped:
        print(f"  skipped {skipped} duplicate row(s) already present")
    return count


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()
    db_path = Path(args.db)
    n = ingest(args.files, db_path)
    print(f"Ingested {n} findings into {db_path}")
