#!/usr/bin/env python3
"""Institutional memory for audit dismissals (Aether SAGE-style).

Cross-run SQLite store of findings that are already dismissed, program-known,
already submitted by us, or accepted as real. Future runs query it so they do
not re-report the same false positive or re-submit a bug we already reported.

Verdicts:
  dismissed  - judged not a real / not payable issue (do not report)
  known      - program-known or out-of-scope (do not report)
  duplicate  - already submitted by us (do not resubmit)
  accepted   - real bug we submitted (future similar = duplicate)

DB defaults to ~/.openaudit/memory/findings.db (override: --db or
OPENAUDIT_MEMORY_DB).

Commands:
  init
  add --protocol P --verdict V --title T [--summary S] [--dup-signature G] ...
  query --protocol P --text "..." [--dup-signature G] [--file F] [--json]
  import-review --review-jsonl out/SLUG/review/review.jsonl [--protocol P]
  stats
  forget --id N | --protocol P --yes
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERDICTS = ("dismissed", "known", "duplicate", "accepted")


def default_db():
    env = os.environ.get("OPENAUDIT_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".openaudit" / "memory" / "findings.db"


def norm_sig(s):
    return re.sub(r"\s+", " ", (s or "").strip()).replace("|", "/").lower()


def tokens(s):
    return re.findall(r"[a-z0-9]{3,}", (s or "").lower())


def _unit(vec):
    n = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / n for k, v in vec.items()}


def text_sim(a, b):
    va, vb = _unit(Counter(tokens(a))), _unit(Counter(tokens(b)))
    return sum(x * vb.get(w, 0.0) for w, x in va.items())


def _same_file(a, b):
    a, b = Path(a or "").name.lower(), Path(b or "").name.lower()
    return bool(a and b) and (a == b or a.endswith(b) or b.endswith(a))


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  protocol TEXT NOT NULL,
  chain TEXT,
  contract TEXT,
  file TEXT,
  vuln_type TEXT,
  dup_signature TEXT,
  title TEXT NOT NULL,
  summary TEXT,
  verdict TEXT NOT NULL,
  severity TEXT,
  reason TEXT,
  evidence TEXT,
  source TEXT,
  run_id TEXT,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mem_proto ON memory(protocol);
CREATE INDEX IF NOT EXISTS idx_mem_sig ON memory(dup_signature);
CREATE INDEX IF NOT EXISTS idx_mem_loc ON memory(protocol, file, vuln_type);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title, summary, reason, content='memory', content_rowid='id',
  tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid,title,summary,reason)
  VALUES (new.id,new.title,new.summary,new.reason);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,summary,reason)
  VALUES ('delete',old.id,old.title,old.summary,old.reason);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,summary,reason)
  VALUES ('delete',old.id,old.title,old.summary,old.reason);
  INSERT INTO memory_fts(rowid,title,summary,reason)
  VALUES (new.id,new.title,new.summary,new.reason);
END;
"""


def connect(path, create=True):
    p = Path(path).expanduser()
    if not p.exists():
        if not create:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS)
    except sqlite3.OperationalError as e:  # FTS5 unavailable - full scan still works
        print(f"[memory] FTS5 unavailable ({e}); using scan", file=sys.stderr)
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(conn, protocol, verdict, title, summary="", dup_signature="", file="",
           vuln_type="", severity="", reason="", evidence="", chain="",
           contract="", source="manual", run_id=""):
    now = _now()
    sig = norm_sig(dup_signature)
    if sig:
        row = conn.execute(
            "SELECT id FROM memory WHERE protocol=? AND dup_signature=?",
            (protocol, sig),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE memory SET last_seen_at=?, times_seen=times_seen+1, "
                "verdict=?, reason=?, severity=?, source=? WHERE id=?",
                (now, verdict, reason, severity, source, row["id"]),
            )
            return row["id"], "updated"
    cur = conn.execute(
        "INSERT INTO memory(protocol,chain,contract,file,vuln_type,dup_signature,"
        "title,summary,verdict,severity,reason,evidence,source,run_id,created_at,"
        "last_seen_at,times_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (protocol, chain, contract, file, vuln_type, sig, title, summary, verdict,
         severity, reason, evidence, source, run_id, now, now),
    )
    return cur.lastrowid, "inserted"


def _row_score(r, text, dup_sig, file, vuln_type):
    if dup_sig and r["dup_signature"] and norm_sig(dup_sig) == r["dup_signature"]:
        s = 1.0
    else:
        s = text_sim(text, " ".join(filter(None, [r["title"], r["summary"], r["reason"]])))
    if file and r["file"] and _same_file(file, r["file"]):
        s = min(1.0, s + 0.10)
    if vuln_type and r["vuln_type"] and vuln_type.strip().lower() == r["vuln_type"].strip().lower():
        s = min(1.0, s + 0.05)
    return s


def memory_matches(db_path, protocol, text, dup_sig="", file="", vuln_type="",
                   min_score=0.5, limit=10):
    conn = connect(db_path, create=False)
    if conn is None:
        return []
    try:
        if protocol:
            rows = conn.execute("SELECT * FROM memory WHERE protocol=?", (protocol,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory").fetchall()
        out = []
        for r in rows:
            s = _row_score(r, text, dup_sig, file, vuln_type)
            if s >= min_score:
                out.append({
                    "id": r["id"], "protocol": r["protocol"], "verdict": r["verdict"],
                    "title": r["title"], "summary": r["summary"], "file": r["file"],
                    "vuln_type": r["vuln_type"], "severity": r["severity"],
                    "reason": r["reason"], "source": r["source"],
                    "times_seen": r["times_seen"], "last_seen_at": r["last_seen_at"],
                    "score": round(s, 3),
                })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:limit]
    finally:
        conn.close()


def cmd_add(args):
    conn = connect(args.db)
    rid, action = upsert(
        conn, protocol=args.protocol, verdict=args.verdict, title=args.title,
        summary=args.summary or "", dup_signature=args.dup_signature or "",
        file=args.file or "", vuln_type=args.vuln_type or "",
        severity=args.severity or "", reason=args.reason or "",
        evidence=args.evidence or "", chain=args.chain or "",
        contract=args.contract or "", source=args.source, run_id=args.run_id or "",
    )
    conn.commit()
    conn.close()
    print(f"[memory] {action} #{rid} ({args.verdict}) {args.title[:70]}")
    return 0


def cmd_query(args):
    rows = memory_matches(args.db, args.protocol, args.text, args.dup_signature or "",
                          args.file or "", args.vuln_type or "", args.min_score, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("[memory] no matches")
        for m in rows:
            print(f"  {m['score']:.2f}  {m['verdict']:<9} #{m['id']}  "
                  f"{m['protocol']}  {m['title'][:70]}"
                  + (f"  [{m['reason'][:60]}]" if m['reason'] else ""))
    return 1 if rows else 0


def cmd_import_review(args):
    p = Path(args.review_jsonl)
    if not p.exists():
        print(f"[memory] not found: {p}", file=sys.stderr)
        return 1
    protocol = args.protocol
    if not protocol:
        try:
            protocol = p.resolve().parents[1].name
        except IndexError:
            print("[memory] pass --protocol", file=sys.stderr)
            return 1
    conn = connect(args.db)
    n = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        verdict = {"duplicate": "duplicate", "known-issue": "known"}.get(o.get("verdict"))
        if not verdict:
            continue
        upsert(
            conn, protocol=protocol, verdict=verdict,
            title=o.get("title") or o.get("id") or "(untitled)",
            summary=o.get("summary") or "", dup_signature=o.get("dup_signature") or "",
            file=o.get("file") or "", vuln_type=o.get("vuln_type") or "",
            severity=o.get("severity") or "",
            reason="review gate: " + "; ".join((o.get("reasons") or [])[:3]),
            source="review_gate", run_id=protocol,
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"[memory] imported {n} verdict(s) for {protocol}")
    return 0


def cmd_init(args):
    connect(args.db).close()
    print(f"[memory] ready: {args.db}")
    return 0


def cmd_stats(args):
    conn = connect(args.db)
    total = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
    print(f"[memory] {args.db}  total={total}")
    for r in conn.execute("SELECT verdict, COUNT(*) c FROM memory GROUP BY verdict ORDER BY c DESC"):
        print(f"  {r['verdict']:<10} {r['c']}")
    for r in conn.execute("SELECT protocol, COUNT(*) c FROM memory GROUP BY protocol ORDER BY c DESC LIMIT 10"):
        print(f"  {r['protocol']:<24} {r['c']}")
    conn.close()
    return 0


def cmd_forget(args):
    if not args.yes:
        print("[memory] pass --yes to confirm deletion", file=sys.stderr)
        return 1
    conn = connect(args.db)
    if args.id:
        cur = conn.execute("DELETE FROM memory WHERE id=?", (args.id,))
    elif args.protocol:
        cur = conn.execute("DELETE FROM memory WHERE protocol=?", (args.protocol,))
    else:
        print("[memory] pass --id or --protocol", file=sys.stderr)
        conn.close()
        return 1
    conn.commit()
    conn.close()
    print(f"[memory] deleted {cur.rowcount} row(s)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(default_db()))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    a = sub.add_parser("add")
    a.add_argument("--protocol", required=True)
    a.add_argument("--verdict", required=True, choices=VERDICTS)
    a.add_argument("--title", required=True)
    a.add_argument("--summary")
    a.add_argument("--dup-signature")
    a.add_argument("--file")
    a.add_argument("--vuln-type")
    a.add_argument("--severity")
    a.add_argument("--reason")
    a.add_argument("--evidence")
    a.add_argument("--chain")
    a.add_argument("--contract")
    a.add_argument("--source", default="manual")
    a.add_argument("--run-id")
    a.set_defaults(func=cmd_add)

    q = sub.add_parser("query")
    q.add_argument("--protocol")
    q.add_argument("--text", required=True)
    q.add_argument("--dup-signature")
    q.add_argument("--file")
    q.add_argument("--vuln-type")
    q.add_argument("--min-score", type=float, default=0.5)
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_query)

    ir = sub.add_parser("import-review")
    ir.add_argument("--review-jsonl", required=True)
    ir.add_argument("--protocol")
    ir.set_defaults(func=cmd_import_review)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    f = sub.add_parser("forget")
    f.add_argument("--id", type=int)
    f.add_argument("--protocol")
    f.add_argument("--yes", action="store_true")
    f.set_defaults(func=cmd_forget)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
