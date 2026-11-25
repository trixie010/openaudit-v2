#!/usr/bin/env python3
"""Heuristic target-intel extractor + EV scorer for a pasted bounty page.

Reads free text (a copied program page), pulls out payouts, TVL, payable
impacts, KYC/response terms, chains, addresses, repos and competition signals,
then writes a transparent 0-100 EV score plus red flags.

    python program_intel.py --slug myproto --text PROGRAM.md
    pbpaste | python program_intel.py --slug myproto --stdin
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request

SEVS = ["critical", "high", "medium", "low"]
CHAINS = [
    "Ethereum", "Arbitrum", "Base", "Optimism", "Polygon", "BNB Smart Chain",
    "BSC", "Avalanche", "Solana", "Sonic", "Blast", "Linea", "Scroll", "zkSync",
    "Cosmos", "NEAR", "Sui", "Aptos", "Tron", "Fantom", "Gnosis", "Celo",
    "Mantle", "Mode", "Sei", "Berachain", "Unichain",
]
HIGH_VALUE = re.compile(
    r"(?i)(theft|steal|drain|freeze|insolven|mint|governance|takeover|"
    r"price|oracle|liquidat|bridge|reentr|permanent|unauthori[sz]ed|"
    r"bypass|arbitrary)"
)
AMT = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmMbB])?"


def parse_amount(num, suf):
    try:
        v = float((num or "0").replace(",", ""))
    except ValueError:
        return 0.0
    s = (suf or "").lower()
    return v * {"k": 1e3, "m": 1e6, "b": 1e9}.get(s, 1.0)


def fmt_usd(v):
    if not v:
        return "$0"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.2f}{suf}".replace(".00", "")
    return f"${v:,.0f}"


def extract_payouts(text):
    out = {}
    for sev in SEVS:
        vals = []
        for m in re.finditer(
            rf"(?i){sev}[^\n]{{0,80}}?\$[ \t]*([0-9][0-9,]*(?:\.[0-9]+)?)[ \t]*([kKmMbB])?",
            text,
        ):
            vals.append(parse_amount(m.group(1), m.group(2)))
        for m in re.finditer(
            rf"(?i){sev}[^\n]{{0,40}}?([0-9]{{1,3}}(?:,[0-9]{{3}})+|[0-9]{{4,}})"
            r"[ \t]*(?:usdc|usdt|dai|usd)?",
            text,
        ):
            vals.append(float(m.group(1).replace(",", "")))
        out[sev] = max(vals) if vals else 0.0
    return out


def extract_tvl(text):
    m = re.search(
        rf"(?i)(?:tvl|total value locked)[^\n$]{{0,60}}?\$[ \t]*([0-9][0-9,]*(?:\.[0-9]+)?)[ \t]*([kKmMbB])?",
        text,
    )
    if not m:
        m = re.search(
            rf"\$[ \t]*([0-9][0-9,]*(?:\.[0-9]+)?)[ \t]*([kKmMbB])?[^\n]{{0,30}}?(?:tvl|total value locked)",
            text,
        )
    return parse_amount(m.group(1), m.group(2)) if m else 0.0


def extract_section(text, start_pat, stop_pat, limit=40):
    start_re = re.compile(start_pat, re.I)
    stop_re = re.compile(stop_pat, re.I)
    bullets, active = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if active and stop_re.match(s):
            active = False
            continue
        if not active and start_re.search(s):
            active = True
            continue
        if active:
            m = re.match(r"^([-*\u2022]|\d+[.)])\s+(.*)", s)
            if m:
                b = m.group(2).strip()
                if b and b not in bullets:
                    bullets.append(b)
                    if len(bullets) >= limit:
                        break
    return bullets


def extract_response_hours(text):
    m = re.search(
        r"(?i)(?:respond|response|first response|triage|resolve)[^\n]{0,40}?(\d+)\s*(hour|hr|day|week)",
        text,
    )
    if not m:
        return None
    mult = {"hour": 1, "hr": 1, "day": 24, "week": 168}[m.group(2).lower()]
    return int(m.group(1)) * mult


def extract_competition(text, label):
    m = re.search(
        rf"(?i)(\d[\d,]*)\s+(?:{label})", text
    ) or re.search(rf"(?i)(?:{label})[^\n\d]{{0,20}}(\d[\d,]*)", text)
    return int(m.group(1).replace(",", "")) if m else None


def fetch_defillama(slug):
    try:
        with urllib.request.urlopen(
            f"https://api.llama.fi/tvl/{slug}", timeout=15
        ) as r:
            return float(r.read().decode().strip())
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort
        print(f"[target-intel] DefiLlama TVL fetch failed: {e}", file=sys.stderr)
        return 0.0


def logcap(v, cap):
    if v <= 0:
        return 0.0
    return min(math.log10(v + 1) / math.log10(cap), 1.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--text", help="path to the pasted program page")
    ap.add_argument("--stdin", action="store_true", help="read page from stdin")
    ap.add_argument("--out", help="output dir (default: out/<slug>)")
    ap.add_argument("--tvl", type=float, help="override TVL in USD")
    ap.add_argument("--defillama", help="DefiLlama protocol slug for TVL")
    ap.add_argument("--participants", type=int)
    ap.add_argument("--submissions", type=int)
    ap.add_argument(
        "--freshness", choices=["new", "recent", "old"], default=None,
        help="code freshness (operator-supplied)",
    )
    args = ap.parse_args()

    if args.stdin:
        text = sys.stdin.read()
    elif args.text:
        text = open(args.text, encoding="utf-8", errors="replace").read()
    else:
        sys.exit("provide --text PATH or --stdin")

    payouts = extract_payouts(text)
    max_payout = max(payouts.values())
    m = re.search(
        r"(?i)(?:maximum|max)\s*(?:bounty|reward|payout)?[^\n]{0,40}?"
        r"\$[ \t]*([0-9][0-9,]*(?:\.[0-9]+)?)[ \t]*([kKmMbB])?",
        text,
    )
    if m:
        max_payout = max(max_payout, parse_amount(m.group(1), m.group(2)))

    tvl = args.tvl if args.tvl else extract_tvl(text)
    if not tvl and args.defillama:
        tvl = fetch_defillama(args.defillama)

    kyc = bool(re.search(r"(?i)\bkyc\b", text)) and not bool(
        re.search(
            r"(?i)(no|not|without)\s+kyc|kyc[^\n]{0,20}(?:not|no)\s+(?:required|needed)",
            text,
        )
    )
    response_hours = extract_response_hours(text)
    chains = [c for c in CHAINS if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    addresses = sorted({a.lower() for a in re.findall(r"0x[a-fA-F0-9]{40}", text)})
    repos = sorted(set(re.findall(r"https?://github\.com/[\w.-]+/[\w.-]+", text)))

    stop = r"^\s*(?:#+\s*)?(?:out of scope|known issues|excluded|severity|payout|reward|kyc|prohibited)"
    in_scope = extract_section(text, r"(?i)(impacts? in scope|in-scope impacts)", stop)
    if not in_scope:
        in_scope = extract_section(text, r"(?i)(assets in scope|in scope)", stop)
    out_scope = extract_section(
        text,
        r"(?i)(out of scope|out-of-scope|known issues|excluded)",
        r"^\s*(?:#+\s*)?(?:severity|payout|reward|kyc|impacts? in scope|assets in scope|in scope)",
    )
    hv_impacts = [b for b in in_scope if HIGH_VALUE.search(b)]

    participants = args.participants if args.participants is not None else extract_competition(
        text, "participants|hunters|researchers"
    )
    submissions = args.submissions if args.submissions is not None else extract_competition(
        text, "submissions|reports|findings"
    )

    payout_s = 30 * logcap(max_payout, 1e6)
    tvl_s = 20 * logcap(tvl, 1e9)
    impact_s = min(len(hv_impacts) / 8, 1.0) * 15
    surface_s = min((len(addresses) + len(repos)) / 6, 1.0) * 5
    response_s = 5 if response_hours and response_hours <= 24 else (
        3 if response_hours and response_hours <= 72 else 0
    )
    fresh_s = {"new": 5, "recent": 3, "old": 0}.get(args.freshness, 2)
    comp_pen = 0.0
    if participants:
        comp_pen = min(participants / 200, 1.0) * 10
    elif submissions:
        comp_pen = min(submissions / 500, 1.0) * 8
    kyc_pen = 5 if kyc else 0
    score = round(
        max(0.0, min(100.0, payout_s + tvl_s + impact_s + surface_s + response_s + fresh_s - comp_pen - kyc_pen)),
        1,
    )
    tier = "A" if score >= 70 else "B" if score >= 50 else "C"

    red_flags = []
    if max_payout == 0:
        red_flags.append("no payout table detected - verify the pasted page")
    if tvl == 0:
        red_flags.append("TVL not detected - pass --tvl or --defillama")
    if participants is None and submissions is None:
        red_flags.append("competition unknown - pass --participants/--submissions")
    if kyc:
        red_flags.append("KYC required (identity friction)")
    if any(re.search(r"(?i)\blow\b|\binfo\b", b) for b in out_scope):
        red_flags.append("Low/Info may be out of scope")
    if re.search(r"(?i)only\s+critical|critical\s+only", text):
        red_flags.append("program may accept ONLY Critical in some domain")
    if not hv_impacts:
        red_flags.append("no theft/freeze-style impacts detected - reread scope")

    breakdown = [
        ("Payout", payout_s, 30, fmt_usd(max_payout)),
        ("TVL", tvl_s, 20, fmt_usd(tvl)),
        ("Payable impacts", impact_s, 15, f"{len(hv_impacts)} high-value / {len(in_scope)} in-scope"),
        ("Surface", surface_s, 5, f"{len(addresses)} addr, {len(repos)} repos"),
        ("Response speed", response_s, 5, f"{response_hours}h" if response_hours else "unknown"),
        ("Freshness", fresh_s, 5, args.freshness or "unknown (neutral)"),
        ("Competition", -comp_pen, 10, f"{participants} participants" if participants else (
            f"{submissions} submissions" if submissions else "unknown")),
        ("KYC", -kyc_pen, 5, "required" if kyc else "no"),
    ]

    out_dir = args.out or os.path.join("out", args.slug)
    os.makedirs(out_dir, exist_ok=True)
    data = {
        "slug": args.slug, "score": score, "tier": tier,
        "payouts": {k: v for k, v in payouts.items()},
        "max_payout": max_payout, "tvl": tvl, "kyc": kyc,
        "response_hours": response_hours, "chains": chains,
        "addresses": addresses, "repos": repos,
        "participants": participants, "submissions": submissions,
        "in_scope": in_scope, "high_value_impacts": hv_impacts,
        "out_of_scope": out_scope,
        "breakdown": [{"component": c, "points": round(p, 1), "max": m_, "note": n} for c, p, m_, n in breakdown],
        "red_flags": red_flags,
    }
    with open(os.path.join(out_dir, "program-intel.json"), "w") as f:
        json.dump(data, f, indent=2)

    lines = [f"# Program intel - {args.slug}", ""]
    lines += [f"**EV score: {score}/100 (Tier {tier})**", ""]
    lines += ["| Component | Points | Max | Basis |", "|---|---|---|---|"]
    for c, p, m_, n in breakdown:
        lines.append(f"| {c} | {p:+.1f} | {m_} | {n} |")
    lines += ["", "## Extracted facts", "", "| Field | Value |", "|---|---|"]
    for sev in SEVS:
        lines.append(f"| {sev.title()} payout | {fmt_usd(payouts[sev])} |")
    lines += [
        f"| Max payout | {fmt_usd(max_payout)} |",
        f"| TVL | {fmt_usd(tvl)} |",
        f"| KYC | {kyc} |",
        f"| Response | {response_hours}h |" if response_hours else "| Response | unknown |",
        f"| Chains | {', '.join(chains) or 'unknown'} |",
        f"| Addresses | {len(addresses)} |",
        f"| Repos | {', '.join(repos) or 'none'} |",
        f"| Participants | {participants if participants is not None else 'unknown'} |",
        f"| Submissions | {submissions if submissions is not None else 'unknown'} |",
    ]
    lines += ["", f"## Payable impacts ({len(in_scope)})", ""]
    lines += [f"- {b}" for b in in_scope] or ["- none detected"]
    lines += ["", f"## High-value impacts ({len(hv_impacts)})", ""]
    lines += [f"- {b}" for b in hv_impacts] or ["- none detected"]
    lines += ["", f"## Out of scope / known issues ({len(out_scope)})", ""]
    lines += [f"- {b}" for b in out_scope] or ["- none detected"]
    lines += ["", "## Red flags", ""]
    lines += [f"- {f}" for f in red_flags] or ["- none"]
    lines += ["", "_Heuristic extraction - verify every number against the page._", ""]
    md_path = os.path.join(out_dir, "program-intel.md")
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    print(f"[target-intel] {args.slug}: score {score}/100 (Tier {tier})")
    print(
        f"  max payout {fmt_usd(max_payout)} | TVL {fmt_usd(tvl)} | KYC {kyc} | "
        f"response {response_hours or '?'}h | high-value impacts {len(hv_impacts)}"
    )
    for f_ in red_flags:
        print(f"  ! {f_}")
    print(f"  -> {md_path}")


if __name__ == "__main__":
    main()
