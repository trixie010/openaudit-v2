#!/usr/bin/env python3
"""LLM judge for `evalctl.py score --judge-cmd`.

Reads the evalctl payload on stdin:

    {"produced": {...}, "truth": {...}, "similarity": 0.27}

Calls an OpenAI-compatible /chat/completions endpoint and prints strict JSON:

    {"match": true|false, "reason": "..."}

Environment:
    JUDGE_BASE_URL   default https://api.openai.com/v1
    JUDGE_API_KEY    required (OPENAI_API_KEY is accepted as a fallback)
    JUDGE_MODEL      default gpt-4o-mini
    JUDGE_TIMEOUT    seconds, default 60

Usage:
    python3 judge_llm.py
    python3 judge_llm.py --self-test
    --judge-cmd "python3 skills/eval-harness/scripts/judge_llm.py"
"""
import argparse
import json
import os
import re
import sys
import urllib.request

SYSTEM = (
    "You grade whether two vulnerability reports describe the SAME root cause. "
    "match=true only when the produced finding identifies the same defect in the same "
    "code path with the same impact class. A different mechanism, a different function, "
    "or merely a related symptom is false. Ignore severity wording and report style. "
    'Reply with strict JSON only: {"match": true|false, "reason": "<one sentence>"}'
)


def trim(payload):
    truth = dict(payload.get("truth") or {})
    produced = dict(payload.get("produced") or {})
    if truth.get("body"):
        truth["body"] = str(truth["body"])[:4000]
    if produced.get("text"):
        produced["text"] = str(produced["text"])[:2000]
    return {
        "answer_key": truth,
        "produced": produced,
        "similarity_hint": payload.get("similarity"),
    }


def build_prompt(payload):
    return json.dumps(trim(payload), ensure_ascii=False, indent=2)


def parse_decision(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in judge output")
    data = json.loads(m.group(0))
    if not isinstance(data.get("match"), bool):
        raise ValueError("judge output has no boolean 'match'")
    return {"match": data["match"], "reason": str(data.get("reason", ""))[:500]}


def call_api(prompt):
    base = os.environ.get("JUDGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
    timeout = float(os.environ.get("JUDGE_TIMEOUT", "60"))
    if not key:
        raise RuntimeError("JUDGE_API_KEY (or OPENAI_API_KEY) is not set")
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser(description="OpenAI-compatible match judge for evalctl")
    ap.add_argument("--self-test", action="store_true",
                    help="validate prompt/parse plumbing without a network call")
    args = ap.parse_args()

    if args.self_test:
        sample = {"truth": {"id": "H-01", "title": "t", "body": "b" * 5000},
                  "produced": {"id": "C-01", "title": "p"}, "similarity": 0.2}
        prompt = build_prompt(sample)
        probe = json.loads(prompt)
        assert "answer_key" in probe and "produced" in probe
        assert len(probe["answer_key"]["body"]) == 4000
        for raw in ('{"match": true, "reason": "ok"}',
                    '```json\n{"match": false, "reason": "different"}\n```'):
            got = parse_decision(raw)
            assert isinstance(got["match"], bool)
        print(json.dumps({"match": True, "reason": "self-test"}))
        return 0

    try:
        payload = json.load(sys.stdin)
        decision = parse_decision(call_api(build_prompt(payload)))
    except Exception as e:
        print(f"judge_llm: {e}", file=sys.stderr)
        return 1
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
