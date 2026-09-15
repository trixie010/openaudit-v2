#!/usr/bin/env python3
"""kb_kritt.py - generate the two-tier KRITT question ledger from the mechanical KB.

Tier M (mechanical, script-owned): derived ONLY from chainscope.db / functions.csv /
reconciliation.md. AGGREGATED BY FAMILY (one row per mechanism+contract, never one row
per function - a per-function ledger explodes past 1000 and drowns the signal).
Pre-triaged: rows whose guard-set is uniform are annotated, not deleted - the agent
may only REFUTE with blocking file:line + the 3-part gate-bypass sub-check.

Tier S (semantic, agent-owned): seeded from invariants.md lines carrying a property-class
tag. Graph-invisible; never skippable; completion requires an answer for each.

Usage:
  python kb_kritt.py --db kb/chainscope.db --functions kb/functions.csv \
      --reconciliation kb/reconciliation.md --invariants kb/invariants.md --out kb/kritt.md
"""
import argparse, csv, json, re, sqlite3
from collections import defaultdict

GUARD_WORDS = ("only", "role", "admin", "auth", "owner", "manager")
CLASS_TAG = re.compile(r"(PROFIT|CANARY|WTOK|ACCUM|COMPLY|STATE_MACHINE|FEE_VOL|DOOMSDAY|"
                       r"NEGATIVE|SIMPLE|INLINE)\b")
SAMPLE_CAP = 6

def fn_label(nid):
    return nid.split("::", 1)[-1].split("(")[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--functions", required=True)
    ap.add_argument("--reconciliation", default=None)
    ap.add_argument("--invariants", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--in-scope-file", help="in-scope-files.txt: only in-scope families become QUEUE rows")
    a = ap.parse_args()

    scope = None
    if a.in_scope_file:
        scope = {l.strip().lstrip("./") for l in open(a.in_scope_file) if l.strip() and not l.startswith("#")}
    def in_scope(path):
        return scope is None or bool(path) and any(path == s or path.startswith(s + "/") or s.startswith(path) for s in scope)
    fn_file = {}
    db = sqlite3.connect(a.db)
    nodes = {}
    for nid, label, typ, vis, f, ls in db.execute(
        "select id,label,type,visibility,file,line_start from nodes"):
        nodes[nid] = dict(label=label, type=typ, vis=vis, file=f, line=ls or 0)

    guards = defaultdict(set)
    for src, tgt in db.execute("select source,target from edges where relation='guards'"):
        guards[tgt].add(fn_label(src))
    writers = defaultdict(set)
    for src, tgt in db.execute("select source,target from edges where relation='writes_state'"):
        writers[tgt].add(src)
    cross_calls = defaultdict(set)  # (caller file, callee file) -> caller fns
    for src, tgt in db.execute("select source,target from edges where relation='calls'"):
        s, t = nodes.get(src), nodes.get(tgt)
        if s and t and s["type"] == "function" and t["type"] == "function" \
                and s["file"] != t["file"] and not fn_label(tgt).startswith("get"):
            cross_calls[(s["file"], t["file"])].add(src)
    sinks = [nid for (nid,) in db.execute(
        "select id from nodes where type='function' and metadata like '%sink%'")]

    canons = list(csv.DictReader(open(a.functions)))
    fn_by_short = {}
    for nid, n in nodes.items():
        if n["type"] == "function":
            fn_by_short.setdefault(n["label"].split(".")[-1], nid)
    canon_mods = {r["function"]: (r.get("modifiers") or "").lower() for r in canons}
    for r in canons:
        fn_file[r["function"].split(".")[-1]] = r.get("file", "")

    def is_guarded(fnspec):
        m = canon_mods.get(fnspec, "")
        if any(w in m for w in GUARD_WORDS):
            return True
        short = fnspec.split(".")[-1]
        return bool(guards.get(fn_by_short.get(short, ""), set()))

    def trigger_of(allfns, mech):
        # ATTACKER-DRIVEN: an ungated public/external fn is in play (anyone can reach it)
        # ROUTINE-OPERATION: every fn in the family is gate-guarded (only the legit holder
        #   calls it normally - which is EXACTLY where privileged-gate downgrades hide)
        if mech == "parser-gap":
            return "ATTACKER-DRIVEN"
        if not allfns:
            return "BOTH"
        g = sum(1 for f in allfns if is_guarded(f))
        if g == len(allfns):
            return "ROUTINE-OPERATION"
        return "ATTACKER-DRIVEN" if g == 0 else "BOTH"

    # ---- aggregates: key = (mechanism, contract/file) -> members ----
    fam = defaultdict(list)

    for var_id, fns in writers.items():
        var = nodes.get(var_id)
        named = sorted({fn_label(x) for x in fns if nodes.get(x, {}).get("type") == "function"})
        if len(named) >= 2:
            fam[("slot-writer", f"{var['file']}::{var['label']}")].append(
                (named, f"{var['file']}:{var['line']}"))

    for (cfile, tfile), callers in cross_calls.items():
        fam[("external-call", cfile)].append(
            (sorted(fn_label(c) for c in callers), f"{cfile} -> {tfile}"))

    for nid in sinks:
        n = nodes.get(nid)
        if n:
            fam[("sink", n["file"])].append(([fn_label(nid)], f"{n['file']}:{n['line']}"))

    # gate-bypass: one row per (contract, guard-name)
    for r in canons:
        mods = (r.get("modifiers") or "")
        nid = fn_by_short.get(r["function"].split(".")[-1])
        for g in guards.get(nid, set()):
            if any(w in g.lower() for w in GUARD_WORDS) or any(
                    w in mods.lower() for w in GUARD_WORDS):
                contract = r["function"].split(".")[0]
                fam[("gate-bypass", f"{contract}::{g or mods}")].append(
                    ([r["function"]], f"{r['file']}:{r['line']}"))

    if a.reconciliation:
        for line in open(a.reconciliation):
            m = re.match(r"^- ([A-Za-z_][\w.]*)", line)
            if m:
                fam[("parser-gap", "kb/reconciliation.md")].append(([m.group(1)], ""))

    # ---- emit ledger rows (queues = in-scope families only) ----
    rows, oos = [], []
    def fam_in_scope(mech, group, allfns):
        if scope is None:
            return True
        cand = [group.split("::")[0], group.split(" ")[0]]
        cand += [fn_file.get(f.split(".")[-1], "") for f in allfns]
        return any(in_scope(p) for p in cand if p)
    for (mech, group), members in sorted(fam.items()):
        allfns = sorted({f for ms, _ in members for f in ms})
        if not fam_in_scope(mech, group, allfns):
            oos.append((mech, group)); continue
        ev = "; ".join({e for _, e in members if e})[:160] or group
        n = len(allfns)
        listed = ", ".join(allfns[:SAMPLE_CAP]) + (f" +{n - SAMPLE_CAP} more" if n > SAMPLE_CAP else "")
        gset = set()
        for f in allfns:
            nid = fn_by_short.get(f.split(".")[-1], "")
            gset |= guards.get(nid, set())
        triage = f" [uniform guards: {','.join(sorted(gset))}]" if len(gset) == 1 else ""
        if mech == "slot-writer":
            q = (f"{group.split('::')[-1]} in {group.split('::')[0]} has writers ({listed}) "
                 "- can one writer move the slot outside the others' intended flow, "
                 "or two in one tx?")
        elif mech == "external-call":
            q = (f"{group} calls cross-file from ({listed}) - for each callee: can it "
                 "revert/return junk and strand caller state?")
        elif mech == "sink":
            q = f"low-level/delegatecall/transfer sinks in {group} ({listed}) - what caller data reaches them, from where?"
        elif mech == "gate-bypass":
            q = (f"role-gated {group.split('::')[-1]} entries in {group.split('::')[0]} "
                 f"({listed}) - GATE-BYPASS sub-check MANDATORY: attacker-satisfiable "
                 "without stolen keys? legit holder triggers harm in normal op? "
                 "harm users even when authorized?")
        else:
            q = f"parser-missed fns ({listed}) - manually confirm callers/writers/guards (reconciliation.md)"
        q += triage
        trig = trigger_of(allfns, mech) if mech != "gate-bypass" else "BOTH"
        rows.append(("M", mech, trig, group, q, ev))

    sem = []
    if a.invariants:
        for line in open(a.invariants):
            if CLASS_TAG.search(line) and re.search(r"\b(can|must|may|will|never)\b", line, re.I):
                sem.append(("S", "invariant", "AGENT-ASSIGN", "kb/invariants.md",
                            line.strip(" -"), "kb/invariants.md"))
    if not sem:
        rows.append(("S", "invariant", "AGENT-ASSIGN", "REQUIRED",
                     "SEMANTIC LAYER EMPTY - invariants.md missing/"
                     "unparseable; agent must author >=15 graph-invisible questions before "
                     "the run may finish", "kb/invariants.md"))
    else:
        rows.extend(sem)

    order = {"slot-writer": 0, "external-call": 1, "sink": 2, "gate-bypass": 3,
             "parser-gap": 4, "invariant": 5}
    rows.sort(key=lambda r: (r[0] == "S", order.get(r[1], 9), r[3]))

    with open(a.out, "w") as f:
        f.write("# KRITT question ledger - generated by kb_kritt.py; do not hand-prune\n\n")
        f.write("Tiers: M = mechanical (script, family-aggregated, pre-triaged), "
                "S = semantic (agent-owned, graph-invisible; AGENT-ASSIGN trigger = set it).\n")
        f.write("TRIGGER column: ATTACKER-DRIVEN (an ungated fn is in play) / ROUTINE-OPERATION "
                "(every fn here is gate-guarded - a legit holder can produce the state in normal "
                "op) / BOTH. SEVERITY FLOOR: a ROUTINE-OPERATION row may NOT be refuted with "
                "'no attacker / only whitelisted callers' - only via the 3-part gate-bypass "
                "answer. This is the P-01-class downgrade the ledger exists to block.\n")
        f.write("Rules: every row leaves UNANSWERED only across session boundaries; REFUTED "
                "requires blocking file:line AND the 3-part gate-bypass answer for any "
                "role-gate refutation; S-rows may never be refuted on 'privileged' alone.\n\n")
        f.write("| id | tier | mechanism | trigger | scope-group | question | evidence | "
                "status | refutation |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        i = 0
        for tier, mech, trig, grp, q, ev in rows:
            i += 1
            f.write(f"| {tier}{i:03d} | {tier} | {mech} | {trig} | {grp} | {q} | {ev} | "
                    f"UNANSWERED | - |\n")
    mc = sum(1 for r in rows if r[0] == "M"); sc = sum(1 for r in rows if r[0] == "S")
    with open(a.out, "a") as f:
        if oos:
            f.write(f"\n<!-- SCOPE: {len(oos)} out-of-scope families excluded from the queue "
                    "by in-scope-files.txt (evidence may still cite them). Do not answer. -->\n")
    print(f"kritt.md: {mc} in-scope M + {sc} S rows; {len(oos)} out-of-scope families excluded")

if __name__ == "__main__":
    main()
