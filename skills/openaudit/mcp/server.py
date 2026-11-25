#!/usr/bin/env python3
"""Minimal stdio MCP server exposing openaudit tools (stdlib only).

Tools: review_candidates, query_memory, record_memory, memory_stats, program_intel.
Speaks JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP stdio transport).
Each tool shells out to the existing CLI so behaviour matches the scripts exactly.

Register in ~/.config/opencode/opencode.jsonc:
  "mcp": { "openaudit": { "type": "local",
      "command": ["python3", "/home/trixie/openaudit/skills/openaudit/mcp/server.py"],
      "enabled": true } }
"""
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REVIEW = REPO / "skills" / "openaudit" / "templates" / "review_gate.py"
DISMISS = REPO / "skills" / "openaudit" / "templates" / "dismissctl.py"
TARGET = REPO / "skills" / "target-intel" / "scripts" / "program_intel.py"
VERSION = "0.1.0"

S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(script, args, input_text=None, timeout=300):
    return subprocess.run(
        [sys.executable, str(script)] + args,
        input=input_text, capture_output=True, text=True,
        cwd=str(REPO), timeout=timeout,
    )


def ok(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def fail(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _abs(p):
    p = Path(p)
    return p if p.is_absolute() else (REPO / p)


def call_review(a):
    slug = a.get("slug") or ""
    cands = a.get("candidates")
    if not cands:
        if not slug:
            raise ValueError("provide 'slug' or 'candidates'")
        cands = str(REPO / "out" / slug / "candidates.csv")
    cands_p = _abs(cands)
    if not cands_p.exists():
        return fail(f"candidates not found: {cands_p}")
    out = _abs(a["out"]) if a.get("out") else cands_p.parent / "review"
    cmd = ["--candidates", str(cands_p), "--out", str(out)]
    if slug:
        cmd += ["--slug", slug]
    for k, flag in (("handoff", "--handoff"), ("prior_audits", "--prior-audits"),
                    ("impact_ruleset", "--impact-ruleset"), ("memory_db", "--memory-db"),
                    ("corpus_db", "--corpus-db"), ("protocol_name", "--protocol-name")):
        if a.get(k):
            cmd += [flag, str(a[k])]
    if a.get("fail_on_blocker"):
        cmd.append("--fail-on-blocker")
    r = run(REVIEW, cmd)
    jl = out / "review.jsonl"
    rows = []
    if jl.exists():
        rows = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines() if x.strip()]
    counts = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    payload = {
        "slug": slug or cands_p.parent.name,
        "counts": counts,
        "review_md": str(out / "review.md"),
        "candidates": rows,
        "stderr": r.stderr.strip()[-1500:],
    }
    return ok(json.dumps(payload, indent=2)) if rows else fail(
        r.stderr.strip() or f"review gate failed (rc={r.returncode})")


def call_query(a):
    if not a.get("text"):
        raise ValueError("'text' is required")
    cmd = ["query", "--text", a["text"], "--json"]
    if a.get("db"):
        cmd = ["--db", a["db"]] + cmd
    for k, flag in (("protocol", "--protocol"), ("dup_signature", "--dup-signature"),
                    ("file", "--file"), ("vuln_type", "--vuln-type")):
        if a.get(k):
            cmd += [flag, str(a[k])]
    if a.get("min_score") is not None:
        cmd += ["--min-score", str(a["min_score"])]
    if a.get("limit") is not None:
        cmd += ["--limit", str(a["limit"])]
    r = run(DISMISS, cmd)
    if r.returncode not in (0, 1):
        return fail(r.stderr.strip() or f"query failed (rc={r.returncode})")
    return ok(r.stdout.strip() or "[]")


def call_record(a):
    for k in ("protocol", "verdict", "title"):
        if not a.get(k):
            raise ValueError(f"'{k}' is required")
    cmd = ["add", "--protocol", a["protocol"], "--verdict", a["verdict"], "--title", a["title"]]
    if a.get("db"):
        cmd = ["--db", a["db"]] + cmd
    for k, flag in (("summary", "--summary"), ("dup_signature", "--dup-signature"),
                    ("file", "--file"), ("vuln_type", "--vuln-type"),
                    ("severity", "--severity"), ("reason", "--reason"),
                    ("evidence", "--evidence"), ("chain", "--chain"),
                    ("contract", "--contract"), ("source", "--source"),
                    ("run_id", "--run-id")):
        if a.get(k):
            cmd += [flag, str(a[k])]
    r = run(DISMISS, cmd)
    return ok(r.stdout.strip() or "recorded") if r.returncode == 0 else fail(
        r.stderr.strip() or f"add failed (rc={r.returncode})")


def call_stats(a):
    cmd = ["stats"]
    if a.get("db"):
        cmd = ["--db", a["db"]] + cmd
    r = run(DISMISS, cmd)
    return ok(r.stdout.strip()) if r.returncode == 0 else fail(
        r.stderr.strip() or f"stats failed (rc={r.returncode})")


def call_intel(a):
    for k in ("slug", "text"):
        if not a.get(k):
            raise ValueError(f"'{k}' is required")
    tmp = tempfile.mkdtemp(prefix="openaudit-intel-")
    try:
        tf = Path(tmp) / "PROGRAM.md"
        tf.write_text(a["text"], encoding="utf-8")
        cmd = ["--slug", a["slug"], "--text", str(tf), "--out", tmp]
        if a.get("tvl") is not None:
            cmd += ["--tvl", str(a["tvl"])]
        if a.get("defillama"):
            cmd += ["--defillama", a["defillama"]]
        if a.get("participants") is not None:
            cmd += ["--participants", str(a["participants"])]
        if a.get("submissions") is not None:
            cmd += ["--submissions", str(a["submissions"])]
        if a.get("freshness"):
            cmd += ["--freshness", a["freshness"]]
        r = run(TARGET, cmd)
        jp = Path(tmp) / "program-intel.json"
        if not jp.exists():
            return fail(r.stderr.strip() or f"program_intel failed (rc={r.returncode})")
        data = json.loads(jp.read_text(encoding="utf-8"))
        data["_stderr"] = r.stderr.strip()[-500:]
        return ok(json.dumps(data, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


TOOLS = [
    {
        "name": "review_candidates",
        "description": "Run the pre-submission Review gate over a candidates.csv (dedup vs handoff/prior/corpus/memory, known issues, impact ruleset, evidence). Returns per-candidate verdicts.",
        "inputSchema": {"type": "object", "properties": {
            "slug": S, "candidates": S, "out": S, "handoff": S, "prior_audits": S,
            "impact_ruleset": S, "memory_db": S, "corpus_db": S, "protocol_name": S,
            "fail_on_blocker": B}},
    },
    {
        "name": "query_memory",
        "description": "Query institutional memory for past dismissed/known/duplicate/accepted findings similar to a candidate. Returns JSON matches (empty array if none).",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": S, "protocol": S, "dup_signature": S, "file": S,
            "vuln_type": S, "min_score": N, "limit": I, "db": S}},
    },
    {
        "name": "record_memory",
        "description": "Record a finding decision in institutional memory (verdict: dismissed|known|duplicate|accepted).",
        "inputSchema": {"type": "object", "required": ["protocol", "verdict", "title"],
                        "properties": {
            "protocol": S, "verdict": {"type": "string",
                "enum": ["dismissed", "known", "duplicate", "accepted"]},
            "title": S, "summary": S, "dup_signature": S, "file": S, "vuln_type": S,
            "severity": S, "reason": S, "evidence": S, "chain": S, "contract": S,
            "source": S, "run_id": S, "db": S}},
    },
    {
        "name": "memory_stats",
        "description": "Show institutional-memory counts by verdict and protocol.",
        "inputSchema": {"type": "object", "properties": {"db": S}},
    },
    {
        "name": "program_intel",
        "description": "Score a pasted bounty program page: extract payouts/TVL/impacts/KYC/competition and return a transparent EV score + red flags.",
        "inputSchema": {"type": "object", "required": ["slug", "text"], "properties": {
            "slug": S, "text": S, "tvl": N, "defillama": S, "participants": I,
            "submissions": I, "freshness": {"type": "string", "enum": ["new", "recent", "old"]}}},
    },
]

HANDLERS = {
    "review_candidates": call_review,
    "query_memory": call_query,
    "record_memory": call_record,
    "memory_stats": call_stats,
    "program_intel": call_intel,
}


def err(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "openaudit", "version": VERSION},
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            return err(mid, -32602, f"unknown tool: {name}")
        try:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": fn(params.get("arguments") or {})}
        except Exception as e:  # noqa: BLE001 - report, never crash the server
            log(traceback.format_exc())
            return {"jsonrpc": "2.0", "id": mid,
                    "result": fail(f"{type(e).__name__}: {e}")}
    if mid is None:
        return None
    return err(mid, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad json: {e}")
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
