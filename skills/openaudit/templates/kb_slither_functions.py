#!/usr/bin/env python3
"""Stage 0.5 KB - canonical function manifest from slither (compiler-grounded).

Run INSIDE the target's foundry project (it compiles via the project's foundry.toml):
    cd out/{slug}/fuzzing/target-work
    uv run --project /path/to/openaudit python /path/to/skills/openaudit/templates/kb_slither_functions.py \
        --out /path/to/out/{slug}/kb/functions.csv

Output: functions.csv  (function,file,line,visibility,modifiers)
This is the CANONICAL manifest (compiler truth) - the coverage matrix audits against it.
Requires: slither importable (openaudit venv), target must compile.
"""
import argparse
import csv
from slither.slither import Slither


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=".", help="target project root (default: cwd)")
    p.add_argument("--out", required=True, help="path to functions.csv")
    p.add_argument("--in-scope-file", help="in-scope-files.txt (SCOPE.md authority); adds in_scope column")
    args = p.parse_args()

    scope = None
    if args.in_scope_file:
        scope = {l.strip().lstrip("./") for l in open(args.in_scope_file) if l.strip() and not l.startswith("#")}
    sl = Slither(args.src)
    rows = set()
    for c in sl.contracts:
        if c.is_interface or c.is_library:
            continue
        for f in c.functions:
            if f.is_implemented and f.visibility in ("public", "external") and not f.name.startswith("_"):
                try:
                    file = f.source_mapping.filename_short
                    line = f.source_mapping.lines[0]
                except Exception:
                    file, line = "?", 0
                mods = ",".join(m.name for m in f.modifiers)
                insc = "true" if (scope is None or (file and (file in scope or any(f0==file for f0 in scope)))) else "false"
                rows.add((f"{c.name}.{f.name}", file, line, f.visibility, mods, insc))

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["function", "file", "line", "visibility", "modifiers", "in_scope"])
        w.writerows(sorted(rows))
    n = sum(1 for r in rows if r[5] == "true")
    print(f"functions.csv: {len(rows)} functions ({n} in-scope, {len(rows)-n} context/excluded)")


if __name__ == "__main__":
    main()
