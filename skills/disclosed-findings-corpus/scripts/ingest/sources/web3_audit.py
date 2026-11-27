"""Ingest the Web3 Audit Corpus release into the disclosed-findings schema.

Source: https://github.com/DicksonWu654/web3-audit-corpus
Data licence: CC BY 4.0 for derived metadata; finding text keeps the publisher's
rights. Keep the original URL with every row and cite it when a hint is used.

The release file is one JSON object per finding. This adapter filters to EVM-ish
High/Medium/Critical findings with usable body text and maps the corpus fields
to the fields the FTS index understands. It does not call an LLM and does not
invent root-cause/pattern text: `pattern` is the title, `root_cause` is a body
excerpt, and the full body is kept for retrieval.

Usage:
    python scripts/ingest/sources/web3_audit.py
    python scripts/ingest/sources/web3_audit.py --firms code4rena sherlock cantina
    python scripts/ingest/ingest_findings.py data/web3_audit.jsonl
"""

import argparse
import gzip
import json
import re
from pathlib import Path

DEFAULT_INPUT = Path.home() / ".local/share/openaudit/corpus/web3-audit/findings.jsonl.gz"
DEFAULT_OUT = Path(__file__).resolve().parents[3] / "data" / "web3_audit.jsonl"

SEVERITIES = {"critical", "high", "medium"}
EVM_CHAIN_MARKERS = {"evm", "unknown", "other: tron", "other: sei"}
EVM_LANG_MARKERS = {"solidity", "vyper", "yul"}

PROTOCOL_HINTS = {
    "lending": ("lending", "borrow", "borrowing", "collateral", "liquidat", "loan", "debt"),
    "vault": ("vault", "erc4626", "share", "deposit", "redeem", "yield"),
    "amm": ("amm", "automated market maker", "swap", "liquidity pool", "dex", "uniswap", "curve"),
    "staking": ("stake", "staking", "validator", "restak", "reward"),
    "bridge": ("bridge", "cross-chain", "crosschain", "layerzero", "wormhole", "ccip", "message"),
    "oracle": ("oracle", "price feed", "chainlink", "twap", "spot price"),
    "governance": ("governance", "proposal", "voting", "dao", "timelock"),
    "nft": ("nft", "erc721", "erc1155", "royalt"),
    "perps": ("perpetual", "perp", "funding rate", "margin"),
    "stablecoin": ("stablecoin", "cdp", "peg", "depeg"),
    "rwa": ("rwa", "real-world asset", "tokenized", "tokenised"),
    "launchpad": ("launchpad", "ido", "presale", "bonding curve"),
}


def _values(value):
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    if value is None:
        return []
    return [str(value)]


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _is_evm_chain(chains):
    if not chains:
        return True
    return bool({c.lower() for c in chains} & EVM_CHAIN_MARKERS)


def _is_evm_lang(languages):
    if not languages:
        return True
    vals = {lang.lower() for lang in languages}
    if vals & EVM_LANG_MARKERS:
        return True
    return vals <= {"unknown", ""}


def _protocol_type(text):
    low = text.lower()
    scores = {}
    for name, words in PROTOCOL_HINTS.items():
        score = sum(low.count(w) for w in words)
        if score:
            scores[name] = score
    if not scores:
        return ""
    return max(scores, key=scores.get)


def _record(raw, args):
    severity = _clean(raw.get("severity")).lower()
    if severity not in args.severity:
        return None

    body = _clean(raw.get("body_text"))
    if len(body) < args.min_body:
        return None

    chains = _values(raw.get("chains"))
    if not args.no_chain_filter and not _is_evm_chain(chains):
        return None

    languages = _values(raw.get("languages"))
    if not args.no_language_filter and not _is_evm_lang(languages):
        return None

    firm = _clean(raw.get("firm_id"))
    if args.firms and firm not in args.firms:
        return None

    title = _clean(raw.get("title"))
    if not title:
        return None

    body = body[: args.max_body]
    return {
        "source": firm or "web3-audit",
        "protocol_type": _protocol_type(f"{title} {body}"),
        "bug_class": _clean(raw.get("vuln_leaf")) or _clean(raw.get("vuln_group")) or "unknown",
        "title": title,
        "root_cause": body[:1200],
        "pattern": title,
        "preconditions": "",
        "severity": severity,
        "fix": "",
        "protocol_name": _clean(raw.get("project")),
        "poc_source": "",
        "body_text": body,
        "source_url": _clean(raw.get("original_url")) or _clean(raw.get("text_source_url")),
        "source_id": _clean(raw.get("finding_uid")),
        "raw_json": {
            "finding_uid": raw.get("finding_uid"),
            "engagement_id": raw.get("engagement_id"),
            "firm_id": raw.get("firm_id"),
            "project": raw.get("project"),
            "audit_year": raw.get("audit_year"),
            "severity_original": raw.get("severity_original"),
            "status": raw.get("status"),
            "vuln_group": raw.get("vuln_group"),
            "vuln_leaf": raw.get("vuln_leaf"),
            "original_url": raw.get("original_url"),
            "text_license": raw.get("text_license"),
            "text_attribution": raw.get("text_attribution"),
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Convert web3-audit-corpus findings to openaudit JSONL.")
    ap.add_argument("--input", default=str(DEFAULT_INPUT))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--severity", nargs="+", default=["critical", "high", "medium"],
                    choices=["critical", "high", "medium", "low", "info", "gas", "unknown"],
                    help="normalised severities to keep (default: critical high medium)")
    ap.add_argument("--firms", nargs="*", default=[], help="firm_id values to keep (default: all)")
    ap.add_argument("--min-body", type=int, default=80, help="minimum cleaned body length")
    ap.add_argument("--max-body", type=int, default=20000, help="cap stored body length")
    ap.add_argument("--limit", type=int, default=0, help="stop after N kept rows (0 = no limit)")
    ap.add_argument("--no-chain-filter", action="store_true", help="keep explicitly non-EVM chains too")
    ap.add_argument("--no-language-filter", action="store_true", help="keep non-EVM languages too")
    args = ap.parse_args()
    args.severity = {s.lower() for s in args.severity}
    args.firms = {f.lower() for f in args.firms}

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"missing corpus file: {src}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    counts = {"scanned": 0, "kept": 0}
    opener = gzip.open if src.suffix == ".gz" else open
    with opener(src, "rt", encoding="utf-8") as fh, out.open("w", encoding="utf-8") as dst:
        for line in fh:
            if not line.strip():
                continue
            counts["scanned"] += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = _record(raw, args)
            if rec is None:
                continue
            dst.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counts["kept"] += 1
            if args.limit and counts["kept"] >= args.limit:
                break

    print(f"scanned={counts['scanned']} kept={counts['kept']} out={out}")


if __name__ == "__main__":
    main()
