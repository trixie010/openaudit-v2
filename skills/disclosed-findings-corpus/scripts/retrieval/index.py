"""
Findings index with two genuinely different query modes - these are not the
same operation run twice, they answer different questions:

- hint_search(): "what do bugs of roughly this shape look like elsewhere?"
  Used at ledger-answering time, scoped by protocol_type, to ground a
  HYPOTHESIS attempt. Abstract/pattern-level matching.

- dedup_search(): "has this finding, on this protocol (or its fork lineage /
  forked-from codebase), already been disclosed?" Used at confirm-time on a
  HYPOTHESIS that passed its PoC, scoped by protocol name/source if known.
  Finding text is exact/specific; protocol_name is a substring filter (so
  "Tornado" also matches "Tornado.Cash"). This is what sets dup_risk.

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


def _terms(value):
    """Normalise one-or-many exclusion substrings to a lowercase tuple."""
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip().lower() for v in value if str(v).strip())


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
    body_text: str = ""
    source_url: str = ""
    source_id: str = ""

    def as_hint(self) -> str:
        """Abstract pattern view - used for hint_search results, deliberately omits
        anything that would let the model just restate the source instead of
        reasoning about the target's own code."""
        body = (self.root_cause or self.body_text).strip()
        if len(body) > 900:
            body = body[:900].rsplit(" ", 1)[0] + " ..."
        return (
            f"[{self.severity.upper()}] {self.bug_class} ({self.source}, {self.protocol_type})\n"
            f"Pattern: {self.pattern or self.title}\n"
            f"Root cause: {body}\n"
            f"Preconditions: {self.preconditions}"
        )

    def as_dedup_record(self) -> str:
        """Specific-match view - used for dedup_search results."""
        text = (
            f"{self.title} ({self.source}{', ' + self.protocol_name if self.protocol_name else ''}) "
            f"[{self.severity.upper()}] {self.bug_class}: {self.root_cause}"
        )
        if self.source_url:
            text += f"\n{self.source_url}"
        return text


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
    def __init__(self, db_path: Path | None = None, exclude_protocol=None, exclude_url=None):
        db_path = Path(db_path) if db_path is not None else DB_PATH
        self._exclude_protocol = _terms(exclude_protocol)
        self._exclude_url = _terms(exclude_url)
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
        self._optional = ("protocol_name", "poc_source", "body_text", "source_url", "source_id")
        self._present = {col: col in cols for col in self._optional}
        self._select_cols = (
            "id, source, protocol_type, bug_class, title, root_cause, pattern, "
            "preconditions, severity, fix"
        )
        for col in self._optional:
            if self._present[col]:
                self._select_cols += f", {col}"
        self._db_path = db_path
        self._count = 0

        tables = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "hint_fts" in tables and "dedup_fts" in tables:
            n = src.execute("SELECT count(*) FROM hint_fts").fetchone()[0]
            if n:
                self.backend = "fts5-disk"
                self._conn = src
                self._count = src.execute("SELECT count(*) FROM findings").fetchone()[0]
                self.findings = []
                return

        rows = src.execute(f"SELECT {self._select_cols} FROM findings").fetchall()
        src.close()
        self.findings = [self._make(r) for r in rows]
        self._count = len(self.findings)
        if not self.findings:
            raise RuntimeError(f"No findings in {db_path}. Run ingest/ingest_findings.py first.")

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
                f"{f.bug_class} {f.protocol_type} {f.title} {f.root_cause} "
                f"{f.pattern} {f.preconditions} {f.body_text[:6000]}",
                f.protocol_type or "",
            )
            for i, f in enumerate(self.findings)
        ]
        dedup_rows = [
            (i, f"{f.title} {f.protocol_name} {f.root_cause} {f.body_text[:4000]}",
             f.protocol_name or "")
            for i, f in enumerate(self.findings)
        ]
        self._conn.executemany("INSERT INTO hint_fts(rowid, body, protocol_type) VALUES (?,?,?)", hint_rows)
        self._conn.executemany("INSERT INTO dedup_fts(rowid, body, protocol_name) VALUES (?,?,?)", dedup_rows)

    def _make(self, row):
        row = list(row)
        for col in self._optional:
            if not self._present[col]:
                row.append("")
        return Finding(*row)

    def _load(self, ids):
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT {self._select_cols} FROM findings WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {r[0]: self._make(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    @staticmethod
    def _dedupe_hits(hits: list[Finding]) -> list[Finding]:
        """Keep the best-ranked row per source/protocol/title.

        The corpus sometimes extracts the same finding from a PDF and its
        markdown twin; without this, top-k burns slots on identical titles.
        """
        seen = set()
        out = []
        for f in hits:
            key = (f.source.lower(), (f.protocol_name or "").lower(), f.title.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    def _is_excluded(self, finding: Finding) -> bool:
        pname = (finding.protocol_name or "").lower()
        url = (finding.source_url or "").lower()
        return (
            any(term in pname for term in self._exclude_protocol)
            or any(term in url for term in self._exclude_url)
        )

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
                if mode == "dedup":
                    sql += f" AND lower({col}) LIKE ?"
                    params.append(f"%{value.lower()}%")
                else:
                    sql += f" AND {col} = ?"
                    params.append(value)
        if self.backend == "fts5-disk" and (self._exclude_protocol or self._exclude_url):
            clauses = []
            if self._exclude_protocol and self._present.get("protocol_name"):
                for term in self._exclude_protocol:
                    clauses.append("lower(protocol_name) LIKE ?")
                    params.append(f"%{term}%")
            if self._exclude_url and self._present.get("source_url"):
                for term in self._exclude_url:
                    clauses.append("lower(source_url) LIKE ?")
                    params.append(f"%{term}%")
            if clauses:
                sql += " AND rowid NOT IN (SELECT id FROM findings WHERE " + " OR ".join(clauses) + ")"
        sql += f" ORDER BY bm25({table}) LIMIT ?"
        params.append(max(top_k, 1) * 4)
        try:
            ids = [r[0] for r in self._conn.execute(sql, params).fetchall()][:top_k]
        except sqlite3.OperationalError:
            return []
        if self.backend == "fts5-disk":
            hits = self._load(ids)
        else:
            hits = [self.findings[i] for i in ids]
            if self._exclude_protocol or self._exclude_url:
                hits = [f for f in hits if not self._is_excluded(f)]
        return self._dedupe_hits(hits)[:top_k]

    def hint_search(self, query: str, protocol_type: str | None = None, top_k: int = 4) -> list[Finding]:
        pf = ("protocol_type", protocol_type) if protocol_type else None
        return self._search(query, "hint", pf, top_k)

    def dedup_search(self, finding_statement: str, protocol_name: str | None = None, top_k: int = 5) -> list[Finding]:
        pf = ("protocol_name", protocol_name) if protocol_name else None
        return self._search(finding_statement, "dedup", pf, top_k)


if __name__ == "__main__":
    idx = FindingsIndex()
    count = getattr(idx, "_count", len(idx.findings))
    print(f"backend: {idx.backend} ({count} findings)")
    for f in idx.hint_search("reward accounting rounding truncation on stake", top_k=3):
        print(f.as_hint())
        print("---")
