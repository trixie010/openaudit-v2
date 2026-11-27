#!/usr/bin/env python3
"""Diff function selectors recovered from bytecode against a verified ABI.

The high-signal output is `bytecode_only`: selectors reachable on the deployed
contract that the verified ABI does not describe (hidden admin/backdoor paths,
stale ABI, proxy/facet mismatch). Works without source using evmole, and falls
back to `cast disassemble` dispatch scanning.

Examples:
  python3 skills/openaudit/scripts/selector_diff.py \
    --address 0xf8B349dA9244253288f6853835e6582955FD49c9 --chain 1 --block 26469201

  python3 skills/openaudit/scripts/selector_diff.py \
    --code 0x6080... --abi out/invusd/abi/Lender.json --slug invusd
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import address_intake as ai  # noqa: E402

FOURBYTE = "https://www.4byte.directory/api/v1/signatures/?hex_signature=0x"


def canon_type(t: dict) -> str:
    typ = t.get("type", "")
    if typ.startswith("tuple"):
        inner = ",".join(canon_type(c) for c in t.get("components", []))
        return f"({inner}){typ[len('tuple'):]}"
    return typ


def abi_signatures(abi: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in abi:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        name = item.get("name")
        if not name:
            continue
        sig = f"{name}({','.join(canon_type(i) for i in item.get('inputs', []))})"
        sel = ai.cast_plain("sig", sig, timeout=20)
        if sel and re.fullmatch(r"0x[0-9a-fA-F]{8}", sel):
            out[sel[2:].lower()] = sig
    return out


def evmole_selectors(code: str) -> dict[str, dict] | None:
    lib = Path.home() / ".local" / "share" / "openaudit" / "pylibs"
    if lib.is_dir() and str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    try:
        import evmole
    except Exception:
        return None
    try:
        info = evmole.contract_info(
            code, selectors=True, arguments=True, state_mutability=True, proxy=True
        )
    except Exception:
        return None
    out: dict[str, dict] = {}
    for fn in info.functions:
        sel = (fn.selector or "").lower()
        sel = sel[2:] if sel.startswith("0x") else sel
        if len(sel) == 8:
            out[sel] = {
                "args": fn.arguments or "",
                "mutability": fn.state_mutability or "",
            }
    return out


def disasm_selectors(code: str) -> tuple[dict[str, dict], bool]:
    raw = ai.cast_plain("disassemble", "--color", "never", code, timeout=90)
    if not raw:
        return {}, False
    ins: list[tuple[str, str]] = []
    for line in raw.splitlines():
        m = re.match(r"^[0-9a-fA-F]+:\s+([A-Z0-9]+)(?:\s+0x([0-9a-fA-F]+))?", line)
        if m:
            ins.append((m.group(1), m.group(2) or ""))
    found: dict[str, dict] = {}
    for i, (op, arg) in enumerate(ins):
        if op == "PUSH4" and len(arg) == 8:
            nxt = [o for o, _ in ins[i + 1:i + 4]]
            if "EQ" in nxt:
                found[arg.lower()] = {"args": "", "mutability": ""}
    heuristic = False
    if not found:
        heuristic = True
        for op, arg in ins:
            if op == "PUSH4" and len(arg) == 8:
                found[arg.lower()] = {"args": "", "mutability": ""}
    return found, heuristic


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--address", default=None)
    ap.add_argument("--code", default=None, help="hex bytecode, or a file path")
    ap.add_argument("--abi", default=None, help="ABI JSON file (fetched from the explorer if omitted)")
    ap.add_argument("--chain", default="1")
    ap.add_argument("--block", default="latest")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--rpc-url", default=None)
    ap.add_argument("--rpc-env", default=None)
    ap.add_argument("--etherscan-key", default=None)
    ap.add_argument("--chains-file", default=str(ai.DEFAULT_CHAINS_FILE))
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--no-evmole", action="store_true")
    ap.add_argument("--allow-no-abi", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if bool(args.address) == bool(args.code):
        sys.exit("provide exactly one of --address or --code")

    ai.load_env()
    chains = ai.load_chains(Path(args.chains_file))
    chain = ai.resolve_chain(args.chain, chains)
    cfg = chains[chain]
    key = (args.etherscan_key or os.environ.get("ETHERSCAN_API_KEY")
           or os.environ.get("ETHERSCAN_KEY"))

    block = 0
    address = None
    if args.address:
        address = ai.checksum(args.address)
        url, _ = ai.rpc_url_for(chain, chains, args)
        rpc = ai.Rpc(url, timeout=args.timeout)
        block = rpc.block_number() if args.block == "latest" else int(args.block)
        code = rpc.code(address, block)
        if code in ("", "0x"):
            sys.exit(f"no code at {address} on chain {chain} block {block}")
    else:
        code = args.code or ""
        if Path(code).is_file():
            code = Path(code).read_text().strip()
        code = code[1:] if code.startswith("@") else code
        if not code.startswith("0x"):
            code = "0x" + code

    abi = None
    if args.abi:
        abi = json.loads(Path(args.abi).read_text())
        if isinstance(abi, dict):
            abi = abi.get("abi")
    elif address:
        src = ai.fetch_source(chain, cfg, address, key)
        if src:
            abi = src.get("abi")
    if not isinstance(abi, list):
        if not args.allow_no_abi:
            sys.exit("no ABI available; pass --abi or --allow-no-abi")
        abi = []

    abi_sigs = abi_signatures(abi)
    recovered = None if args.no_evmole else evmole_selectors(code)
    if recovered is not None:
        source = "evmole"
        heuristic = False
    else:
        recovered, heuristic = disasm_selectors(code)
        source = "disassembly-heuristic" if heuristic else "disassembly-dispatch"
    if not recovered:
        sys.exit("could not recover any selectors from the bytecode")

    both = sorted(set(recovered) & set(abi_sigs))
    bytecode_only = sorted(set(recovered) - set(abi_sigs))
    abi_only = sorted(set(abi_sigs) - set(recovered))

    label = args.slug or (address[:10] if address else "bytecode")
    if args.out:
        out_dir = Path(args.out)
    elif args.slug:
        out_dir = ai.ROOT / "out" / args.slug
    elif address:
        out_dir = ai.ROOT / "out" / f"address-{address[2:10].lower()}"
    else:
        out_dir = Path.cwd() / "selector-diff"
    report = out_dir / "reports" / f"selector-diff-{label}.md"
    report.parent.mkdir(parents=True, exist_ok=True)

    def row(sel: str) -> str:
        meta = recovered.get(sel, {})
        return (f"| `0x{sel}` | {meta.get('args', '') or '-'} | "
                f"{meta.get('mutability', '') or '-'} | [4byte]({FOURBYTE}0x{sel}) |")

    lines = [
        f"# Selector diff - {label}", "",
        f"- Target: `{address or 'bytecode'}`",
        f"- Chain: `{chain}` ({cfg['name']}), block `{block}`",
        f"- Selector source: `{source}`",
        f"- Bytecode selectors: {len(recovered)} | ABI selectors: {len(abi_sigs)} | "
        f"matched: {len(both)}", "",
        "## In bytecode but NOT in the ABI (highest signal)", "",
        "| selector | args | mutability | lookup |", "|---|---|---|---|",
    ]
    lines += [row(s) for s in bytecode_only] or ["| - | - | - | - |"]
    lines += ["", "## In the ABI but NOT in bytecode (stale/inherited)", "",
              "| selector | signature |", "|---|---|"]
    lines += [f"| `0x{s}` | `{abi_sigs[s]}` |" for s in abi_only] or ["| - | - |"]
    lines += ["", "## Matched", "", "| selector | signature | args | mutability |",
              "|---|---|---|---|"]
    lines += [
        f"| `0x{s}` | `{abi_sigs[s]}` | {recovered[s].get('args', '') or '-'} | "
        f"{recovered[s].get('mutability', '') or '-'} |"
        for s in both
    ] or ["| - | - | - | - |"]
    lines.append("")
    report.write_text("\n".join(lines))

    result = {
        "target": address or "bytecode", "chain": chain, "block": block,
        "source": source, "counts": {
            "bytecode": len(recovered), "abi": len(abi_sigs),
            "matched": len(both), "bytecode_only": len(bytecode_only),
            "abi_only": len(abi_only),
        },
        "bytecode_only": [
            {"selector": "0x" + s, **recovered[s], "signature": abi_sigs.get(s)}
            for s in bytecode_only
        ],
        "abi_only": [{"selector": "0x" + s, "signature": abi_sigs[s]} for s in abi_only],
        "matched": [{"selector": "0x" + s, "signature": abi_sigs[s]} for s in both],
    }
    (report.parent / f"selector-diff-{label}.json").write_text(json.dumps(result, indent=2))
    print(f"[selector-diff] {report}", file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
