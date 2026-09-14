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
    a = ap.parse_args()

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

    # ---- emit ledger rows ----
    rows = []
    for (mech, group), members in sorted(fam.items()):
        allfns = sorted({f for ms, _ in members for f in ms})
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
        rows.append(("M", mech, group, q, ev))

    sem = []
    if a.invariants:
        for line in open(a.invariants):
            if CLASS_TAG.search(line) and re.search(r"\b(can|must|may|will|never)\b", line, re.I):
                sem.append(("S", "invariant", "kb/invariants.md", line.strip(" -"), "kb/invariants.md"))
    if not sem:
        rows.append(("S", "invariant", "REQUIRED", "SEMANTIC LAYER EMPTY - invariants.md missing/"
                     "unparseable; agent must author >=15 graph-invisible questions before "
                     "the run may finish", "kb/invariants.md"))
    else:
        rows.extend(sem)

    order = {"slot-writer": 0, "external-call": 1, "sink": 2, "gate-bypass": 3,
             "parser-gap": 4, "invariant": 5}
    rows.sort(key=lambda r: (r[0] == "S", order.get(r[1], 9), r[2]))

    with open(a.out, "w") as f:
        f.write("# KRITT question ledger - generated by kb_kritt.py; do not hand-prune\n\n")
        f.write("Tiers: M = mechanical (script, family-aggregated, pre-triaged), "
                "S = semantic (agent-owned, graph-invisible).\n")
        f.write("Rules: every row leaves UNANSWERED only across session boundaries; REFUTED "
                "requires blocking file:line AND the 3-part gate-bypass answer for any "
                "role-gate refutation; S-rows may never be refuted on 'privileged' alone.\n\n")
        f.write("| id | tier | mechanism | scope-group | question | evidence | status | refutation |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        i = 0
        for tier, mech, grp, q, ev in rows:
            i += 1
            f.write(f"| {tier}{i:03d} | {tier} | {mech} | {grp} | {q} | {ev} | UNANSWERED | - |\n")
    mc = sum(1 for r in rows if r[0] == "M"); sc = sum(1 for r in rows if r[0] == "S")
    print(f"kritt.md: {mc} mechanical family-rows + {sc} semantic rows -> {a.out}")

if __name__ == "__main__":
    main()
