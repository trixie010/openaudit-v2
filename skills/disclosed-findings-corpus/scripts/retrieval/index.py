"""
Findings index with two genuinely different query modes - these are not the
same operation run twice, they answer different questions:

- hint_search(): "what do bugs of roughly this shape look like elsewhere?"
  Used at ledger-answering time, scoped by protocol_type, to ground a
  HYPOTHESIS attempt. Abstract/pattern-level matching.

- dedup_search(): "has this exact finding, on this protocol (or its fork
  lineage / forked-from codebase), already been disclosed?" Used at
  confirm-time on a HYPOTHESIS that passed its PoC, scoped by protocol name/
  source if known. Exact/specific matching - this is what sets dup_risk.

Backend: SQLite FTS5 (bm25 ranking), which ships with Python's stdlib sqlite3 -
ZERO third-party dependencies. No sklearn/scipy/numpy, no embeddings API, no
network calls at query time. If your Python's sqlite3 lacks FTS5 it raises a
clear error telling you so; virtually every modern build has it.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "findings.db"  # scripts/retrieval/ -> skill root / data/

_WORD = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class Finding:
    id: int
    source: str
    protocol_type: str
    bug_class: str
    title: str
    root_cause: str
    pattern: str
    preconditions: str
    severity: str
    fix: str
    protocol_name: str = ""
    poc_source: str = ""

    def as_hint(self) -> str:
        """Abstract pattern view - used for hint_search results, deliberately omits
        anything that would let the model just restate the source instead of
        reasoning about the target's own code."""
        return (
            f"[{self.severity.upper()}] {self.bug_class} ({self.source}, {self.protocol_type})\n"
            f"Pattern: {self.pattern}\n"
            f"Root cause: {self.root_cause}\n"
            f"Preconditions: {self.preconditions}"
        )

    def as_dedup_record(self) -> str:
        """Specific-match view - used for dedup_search results."""
        return (
            f"{self.title} ({self.source}{', ' + self.protocol_name if self.protocol_name else ''}) "
            f"[{self.severity.upper()}] {self.bug_class}: {self.root_cause}"
        )


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    FTS5 treats characters like ( ) : * " - as syntax, so raw code or a bug
    statement would raise a syntax error. Extract word tokens and OR them as
    quoted phrases - recall-friendly, and each token is safely quoted.
    """
    words = _WORD.findall(text or "")
    if not words:
        return ""
    return " OR ".join(f'"{w}"' for w in words)


class FindingsIndex:
    def __init__(self, db_path: Path = DB_PATH):
        db_path = Path(db_path)
        if not db_path.exists():
            raise RuntimeError(
                f"No findings DB at {db_path}. Run ingest/ingest_findings.py first "
                f"(e.g. python scripts/ingest/ingest_findings.py data/sample_findings.jsonl)."
            )
        src = sqlite3.connect(db_path)
        cols = [r[1] for r in src.execute("PRAGMA table_info(findings)").fetchall()]
        if not cols:
            src.close()
            raise RuntimeError(f"{db_path} has no `findings` table; re-run ingest_findings.py.")
        has_protocol_name = "protocol_name" in cols
        has_poc_source = "poc_source" in cols
        select_cols = "id, source, protocol_type, bug_class, title, root_cause, pattern, preconditions, severity, fix"
        if has_protocol_name:
            select_cols += ", protocol_name"
        if has_poc_source:
            select_cols += ", poc_source"
        rows = src.execute(f"SELECT {select_cols} FROM findings").fetchall()
        src.close()

        def _make(row):
            row = list(row)
            if not has_protocol_name:
                row.append("")
            if not has_poc_source:
                row.append("")
            return Finding(*row)

        self.findings: list[Finding] = [_make(r) for r in rows]
        if not self.findings:
            raise RuntimeError(f"No findings in {db_path}. Run ingest/ingest_findings.py first.")

        # Build two in-memory FTS5 tables, rowid == position in self.findings.
        self.backend = "fts5"
        self._conn = sqlite3.connect(":memory:")
        try:
            self._conn.execute("CREATE VIRTUAL TABLE hint_fts USING fts5(body, protocol_type UNINDEXED)")
            self._conn.execute("CREATE VIRTUAL TABLE dedup_fts USING fts5(body, protocol_name UNINDEXED)")
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                "SQLite FTS5 is not available in this Python build "
                f"({e}). Install a Python linked against SQLite with FTS5 enabled."
            ) from e

        hint_rows = [
            (
                i,
                f"{f.bug_class} {f.protocol_type} {f.title} {f.root_cause} {f.pattern} {f.preconditions}",
                f.protocol_type or "",
            )
            for i, f in enumerate(self.findings)
        ]
        dedup_rows = [
            (i, f"{f.title} {f.protocol_name} {f.root_cause}", f.protocol_name or "")
            for i, f in enumerate(self.findings)
        ]
        self._conn.executemany("INSERT INTO hint_fts(rowid, body, protocol_type) VALUES (?,?,?)", hint_rows)
        self._conn.executemany("INSERT INTO dedup_fts(rowid, body, protocol_name) VALUES (?,?,?)", dedup_rows)

    def _search(self, query: str, mode: str, protocol_filter: tuple[str, str] | None, top_k: int) -> list[Finding]:
        match = _fts_query(query)
        if not match:
            return []
        table = "hint_fts" if mode == "hint" else "dedup_fts"
        col = "protocol_type" if mode == "hint" else "protocol_name"
        sql = f"SELECT rowid FROM {table} WHERE {table} MATCH ?"
        params: list = [match]
        if protocol_filter:
            field, value = protocol_filter
            if field == col and value:
                sql += f" AND {col} = ?"
                params.append(value)
        sql += f" ORDER BY bm25({table}) LIMIT ?"
        params.append(max(top_k, 1) * 4 if protocol_filter else max(top_k, 1))
        try:
            ids = [r[0] for r in self._conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            return []
        return [self.findings[i] for i in ids[:top_k]]

    def hint_search(self, query: str, protocol_type: str | None = None, top_k: int = 4) -> list[Finding]:
        pf = ("protocol_type", protocol_type) if protocol_type else None
        return self._search(query, "hint", pf, top_k)

    def dedup_search(self, finding_statement: str, protocol_name: str | None = None, top_k: int = 5) -> list[Finding]:
        pf = ("protocol_name", protocol_name) if protocol_name else None
        return self._search(finding_statement, "dedup", pf, top_k)


if __name__ == "__main__":
    idx = FindingsIndex()
    print(f"backend: {idx.backend} ({len(idx.findings)} findings)")
    for f in idx.hint_search("reward accounting rounding truncation on stake", top_k=3):
        print(f.as_hint())
        print("---")
