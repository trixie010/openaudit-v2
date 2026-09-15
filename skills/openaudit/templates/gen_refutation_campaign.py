#!/usr/bin/env python3
"""gen_refutation_campaign.py - turn REFUTED/BLOCKED/LEAD ledger rows into a
refutation-falsification fuzz campaign (the fuzzer attacks the agents' "no"s).

Inputs (private workspace): kb/kritt.md, candidates.csv, kb/functions.csv, kb/access-control.md
Outputs:
  private/queue.json      - full provenance incl. file:line + blocking citation (NEVER publish)
  private/handler-plan.md - per-function handler signatures to implement, ranked by priority
  public/campaign.md      - sanitized families-only brief (safe for the PUBLIC fuzz-lab)

Priority tiers (falsification value, high->low):
  1 gate-bypass rows REFUTED on 'role-gated'      (ROUTINE-OPERATION floor: routine holders only)
  2 other REFUTED rows                            (any 'no input can break this' claim)
  3 candidates.csv LEAD/BLOCKED verdicts          (unresolved hypotheses)
Only rows whose target function is in kb/functions.csv with in_scope=true (else skipped).
"""
import argparse, csv, json, os, re, sys
from collections import OrderedDict

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, help="out/{slug}")
    p.add_argument("--top", type=int, default=30, help="rows per tier into the queue")
    a = p.parse_args()
    ws = a.workspace.rstrip("/")
    kb = os.path.join(ws, "kb")

    # authoritative scope = in-scope-files.txt (never functions.csv - it is derived, and may
    # be stale/broken); functions.csv only supplies signatures/modifiers as a lookup
    scope_files = set()
    isf = os.path.join(ws, "in-scope-files.txt")
    if os.path.exists(isf):
        scope_files = {l.strip().lstrip("./") for l in open(isf) if l.strip()}
    funcs = {}
    fcsv = os.path.join(kb, "functions.csv")
    if os.path.exists(fcsv):
        try:
            for r in csv.DictReader(open(fcsv)):
                if r.get("function"):
                    funcs[r["function"].split(".")[-1]] = r
        except Exception:
            pass
    queue, seen = [], set()
    def push(tier, row_id, func, fpath, claim, note, fns=None):
        f = funcs.get(func)
        fpath = fpath or (f["file"] if f else "")
        # file-level scope gate against the authoritative program-scope list
        if fpath and scope_files and not any(fpath == s0 or fpath.startswith(s0) or s0.endswith(fpath) for s0 in scope_files):
            return
        handler = func if f else (fns[0] if fns else func)
        key = (handler, fpath)
        if key in seen: return
        seen.add(key)
        queue.append({"tier": tier, "row": row_id, "function": handler,
                      "candidates": (fns or [])[:8], "file": fpath,
                      "modifiers": (f or {}).get("modifiers", ""),
                      "claim": claim, "blocking_citation": note})

    # tier 1+2 from kritt.md
    kritt = os.path.join(kb, "kritt.md")
    if os.path.exists(kritt):
        for line in open(kritt):
            if not line.startswith("|"): continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) < 9 or not re.match(r"^[MS]\d+$", cols[0]): continue
            rid, mech, group, q, status, ref = cols[0], cols[2], cols[4], cols[5], cols[7], cols[8]
            if "REFUTED" not in status.upper(): continue
            blob = ref + " " + q
            m = re.search(r"(\S+\.sol):(\d+)?", ref) or re.search(r"(src/\S+?\.sol)", q)
            fpath = m.group(1).lstrip("./") if m else ""
            cands = [f"{c}.{fn}" for c, fn in re.findall(r"([A-Za-z_]\w+)\.([A-Za-z_]\w+)\(", blob)]
            if not cands:
                w = re.findall(r"[A-Za-z_]\w+", group.split("::")[-1])
                cands = [x for x in w if x in funcs]
            fn = next((c.split(".")[-1] for c in cands if c.split(".")[-1] in funcs),
                      cands[0].split(".")[-1] if cands else group.split("::")[-1])
            claim = re.sub(r"\s+", " ", q)[:240]
            tier = 1 if mech == "gate-bypass" else 2
            push(tier, rid, fn, fpath, claim, ref[:200], [c.split(".")[-1] for c in cands])

    # tier 3 from candidates.csv
    ccsv = os.path.join(ws, "candidates.csv")
    if os.path.exists(ccsv):
        for r in csv.DictReader(open(ccsv)):
            v = (r.get("referee_verdict") or "").upper()
            if v.startswith("LEAD") or v.startswith("BLOCKED"):
                fn = (r.get("summary") or "").split("(")[0][:60]
                tier = 1 if "gated" in v.lower() or "role" in (r.get("submission_advice") or "").lower() else 3
                push(tier, r.get("id", "?"), r.get("line", "") and fn or fn,
                     r.get("file", ""), fn, v + " " + (r.get("referee_verdict") or "")[:160])

    # dedupe per-tier caps
    out = []
    for tier in (1, 2, 3):
        rows = [e for e in queue if e["tier"] == tier]
        out.extend(rows[:a.top])
    queue = out

    o = os.path.join(ws, "fuzzing", "campaign-refutation")
    os.makedirs(os.path.join(o, "private"), exist_ok=True)
    os.makedirs(os.path.join(o, "public"), exist_ok=True)
    json.dump({"source": os.path.basename(ws), "generated": __import__("datetime").date.today().isoformat(),
               "rule": "only ROUTINE-OPERATION actors (legit holder doing its normal job via prank/grantRole); "
                       "never leaked keys, never governance multisig actions - matches SCOPE privilege exclusion "
                       "and the ROUTINE floor",
               "queue": queue}, open(os.path.join(o, "private", "queue.json"), "w"), indent=2)

    hp = ["# Handler plan (implement top-N in the props contracts; one handler per row family)\n"]
    fam = OrderedDict()
    for e in queue:
        fam.setdefault((e["file"], e["function"]), []).append(e["row"])
    for (fp, fn), rows in list(fam.items())[:a.top]:
        hp.append(f"- `fn_{re.sub(r'[^A-Za-z0-9]','_',fn)}` -> prank routine-holder actor; call `{fn}` ({fp}) "
                  f"args: fuzzed incl. 0/MAX/dust; rows: {', '.join(rows[:6])}")
    open(os.path.join(o, "private", "handler-plan.md"), "w").write("\n".join(hp) + "\n")

    # sanitized public brief: family labels only, no paths, no citations
    lines = ["# Refutation-falsification campaign (generic)\n",
             "Class-level invariant: user value unchanged and exits functional after arbitrary *routine* "
             "holder operations (keepers, signers, liquidators performing normal protocol duties).\n",
             f"{len(queue)} queued targets across tiers: "
             f"{sum(1 for e in queue if e['tier']==1)} role-gate claims, "
             f"{sum(1 for e in queue if e['tier']==2)} mechanical refutations, "
             f"{sum(1 for e in queue if e['tier']==3)} open hypotheses.\n"]
    # NO function names in the public brief - a list of specific unpatched functions is itself
    # a target map. Public = class-level + tier counts only; handler detail stays in private/.
    lines.append("- handler roster and per-function focus: private/handler-plan.md (never published)")
    open(os.path.join(o, "public", "campaign.md"), "w").write("\n".join(lines) + "\n")
    print(f"queue: {len(queue)} rows -> {o}/private (FULL provenance - do not publish) "
          f"+ /public/campaign.md (sanitized)")

if __name__ == "__main__":
    main()
