#!/usr/bin/env python3
"""Stage 0.5 KB - graph layer + reconciliation from a ChainScope database.

Run anywhere (needs only python3 + sqlite3 stdlib):
    python3 kb_from_chainscope_db.py \
        --db out/{slug}/kb/chainscope.db \
        --out out/{slug}/kb \
        [--functions-csv out/{slug}/kb/functions.csv]

Produces (into --out):
    graph-functions.csv  every function in the ChainScope graph (parser layer)
    dependency-map.md    per-function storage reads/writes/calls (mechanical)
    reconciliation.md    compiler-vs-parser diff (parser gaps flagged) - only with --functions-csv

chainscope.db is produced first by: cs_build.py {src} --db <kb>/chainscope.db
"""
import argparse
import csv
import os
import sqlite3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="path to chainscope.db")
    p.add_argument("--out", required=True, help="KB output directory")
    p.add_argument("--functions-csv", help="canonical functions.csv (enables reconciliation)")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)
    db = sqlite3.connect(args.db)

    # 1) graph-functions.csv
    rows = db.execute(
        "select label, file, line_start, visibility from nodes where type='function' order by file, line_start"
    ).fetchall()
    with open(os.path.join(args.out, "graph-functions.csv"), "w") as f:
        f.write("function,file,line,visibility\n")
        for r in rows:
            f.write(",".join(str(x).replace(",", ";") for x in r) + "\n")
    print(f"graph-functions.csv: {len(rows)} functions")

    # 2) dependency-map.md
    nodes = dict(db.execute("select id,label from nodes").fetchall())

    def rel(kind: str):
        m: dict = {}
        for s, t in db.execute("select source,target from edges where relation=?", (kind,)):
            m.setdefault(nodes.get(s, "?"), []).append(nodes.get(t, "?"))
        return m

    calls, reads, writes = rel("calls"), rel("reads_state"), rel("writes_state")
    with open(os.path.join(args.out, "dependency-map.md"), "w") as f:
        f.write("# Dependency map (mechanical, from ChainScope graph)\n\n")
        f.write("Per state-changing function: storage reads/writes + outgoing calls.\n\n")
        count = 0
        for label, file, line, vis in rows:
            if vis not in ("public", "external") or label.startswith("_"):
                continue
            r = sorted(set(reads.get(label, [])))
            w = sorted(set(writes.get(label, [])))
            c = sorted(set(calls.get(label, [])))
            if not (r or w):
                continue
            count += 1
            f.write(f"### {label}  ({file}:{line}, {vis})\n")
            if w:
                f.write("- writes: " + ", ".join(w) + "\n")
            if r:
                f.write("- reads: " + ", ".join(r) + "\n")
            if c:
                f.write("- calls: " + ", ".join(c[:12]) + (" ...\n" if len(c) > 12 else "\n"))
    print(f"dependency-map.md: {count} entries")

    # 3) reconciliation.md (compiler truth vs parser graph)
    if args.functions_csv:
        sl = {r["function"].split(".")[-1] for r in csv.DictReader(open(args.functions_csv))}
        cs = {label.split(".")[-1] for (label,) in db.execute("select label from nodes where type='function'")}
        missing = sorted(n for n in sl if n not in cs)
        with open(os.path.join(args.out, "reconciliation.md"), "w") as f:
            f.write("# Manifest reconciliation (compiler vs parser)\n\n")
            f.write(f"- slither canonical state-changing fns (name universe): {len(sl)}\n")
            f.write(f"- chainscope graph fns: {len(cs)}\n")
            f.write(f"- compiler functions missing from graph: {len(missing)}\n\n")
            f.write("These are parser gaps - flagged so no function is invisible. Names:\n")
            f.write("\n".join("- " + m for m in missing))
        print(f"reconciliation.md: {len(missing)} parser gaps flagged")


if __name__ == "__main__":
    main()
