#!/usr/bin/env python3
"""`opencode run` backend for `evalctl.py score --judge-cmd`.

Same stdin/stdout contract as judge_llm.py, but the classification is made by a
no-tool opencode agent instead of a direct API call. This reuses the provider and
auth already configured for opencode, so no separate JUDGE_API_KEY is needed.

Setup (one-time):
    ln -s "$PWD/skills/eval-harness/agents/eval-judge.md" \
          ~/.config/opencode/agents/eval-judge.md

Because `opencode run` starts a fresh process, it picks up the agent file on the
next invocation; no restart of the parent session is required.

Usage:
    python3 judge_opencode.py
    python3 judge_opencode.py --dry-run < payload.json
    python3 judge_opencode.py --self-test
    --judge-cmd "python3 skills/eval-harness/scripts/judge_opencode.py"

Environment:
    JUDGE_OPENCODE_AGENT    default eval-judge
    JUDGE_OPENCODE_MODEL    optional provider/model (default: opencode default)
    JUDGE_OPENCODE_DIR      scratch dir, default /tmp/opencode-judge
    JUDGE_OPENCODE_TIMEOUT  seconds, default 120
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_llm  # noqa: E402

DEFAULT_AGENT = "eval-judge"
DEFAULT_DIR = "/tmp/opencode-judge"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _collect_text(node):
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "content") and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_collect_text(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_text(item))
    return out


def extract_decision(raw):
    try:
        return judge_llm.parse_decision(raw)
    except Exception:
        pass
    chunks = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunks.extend(_collect_text(json.loads(line)))
        except Exception:
            continue
    if chunks:
        try:
            return judge_llm.parse_decision("\n".join(chunks))
        except Exception:
            pass
    return judge_llm.parse_decision(ANSI.sub("", raw or ""))


def build_cmd(agent, model, workdir, prompt, pure=True):
    cmd = ["opencode", "run"]
    if pure:
        cmd.append("--pure")
    cmd += ["--format", "json", "--agent", agent, "--dir", workdir]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


def main():
    ap = argparse.ArgumentParser(description="opencode-run match judge for evalctl")
    ap.add_argument("--agent", default=os.environ.get("JUDGE_OPENCODE_AGENT", DEFAULT_AGENT))
    ap.add_argument("--model", default=os.environ.get("JUDGE_OPENCODE_MODEL", ""))
    ap.add_argument("--workdir", default=os.environ.get("JUDGE_OPENCODE_DIR", DEFAULT_DIR))
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("JUDGE_OPENCODE_TIMEOUT", "120")))
    ap.add_argument("--no-pure", action="store_true", help="keep external plugins loaded")
    ap.add_argument("--with-mcp", action="store_true", help="do not disable MCP servers")
    ap.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    ap.add_argument("--self-test", action="store_true", help="validate plumbing, do not run")
    args = ap.parse_args()

    raw = sys.stdin.read()
    if raw.strip():
        payload = json.loads(raw)
    else:
        payload = {"truth": {"id": "H-01", "title": "t", "body": "b"},
                   "produced": {"id": "C-01", "title": "p"}, "similarity": 0.2}

    prompt = judge_llm.build_prompt(payload)
    cmd = build_cmd(args.agent, args.model, args.workdir, prompt,
                    pure=not args.no_pure)

    if args.self_test:
        probe = json.loads(prompt)
        assert "answer_key" in probe and "produced" in probe
        assert extract_decision('{"match": true, "reason": "ok"}')["match"] is True
        events = '\n'.join([
            json.dumps({"type": "step_start", "sessionID": "s"}),
            json.dumps({"type": "text", "part": {"type": "text",
                                                 "text": '{"match": false, "reason": "x"}'}}),
        ])
        assert extract_decision(events)["match"] is False
        print(json.dumps({"match": True, "reason": "self-test-opencode"}))
        return 0

    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "agent": args.agent,
            "model": args.model or None,
            "workdir": args.workdir,
            "prompt_chars": len(prompt),
            "cmd": [c if len(c) < 120 else c[:117] + "..." for c in cmd],
        }))
        return 0

    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if not args.with_mcp:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"mcp": {"openaudit": {"enabled": False}}})

    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=args.workdir, env=env, capture_output=True,
                              text=True, timeout=args.timeout)
    except FileNotFoundError:
        print("judge_opencode: 'opencode' not found on PATH", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"judge_opencode: timed out after {args.timeout:.0f}s", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        print(f"judge_opencode: exit {proc.returncode}: {tail}", file=sys.stderr)
        return 1

    try:
        decision = extract_decision(proc.stdout)
    except Exception as e:
        tail = (proc.stdout or "").strip()[-500:]
        print(f"judge_opencode: unparseable output ({e}): {tail}", file=sys.stderr)
        return 1

    decision["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
