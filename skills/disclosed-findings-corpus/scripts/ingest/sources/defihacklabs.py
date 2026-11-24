"""
DeFiHackLabs ingester - real exploit PoCs as actual Foundry tests, not audit
report text. Tested against the real cloned repo while building this (not
guessed): ~67% of files use a consistent `// @KeyInfo - Total Lost : ...` /
`// Attacker : ...` / `// Vulnerable Contract : ...` / `// Attack Tx : ...`
header block; a smaller number use an older `// reason :` style; about 31%
have no structured header at all.

These PoCs don't state WHY the exploit worked (no root-cause text) - they're
working attack code, not audit findings. Two tiers:
1. Always: parse whatever header metadata exists, keep the raw Solidity
   source as `poc_source` - this is the main value, used as a few-shot style
   reference when the calling agent writes PoCs, independent of classification.
2. bug_class/protocol_type/root_cause/pattern are left EMPTY by this script -
   filling them in is the calling agent's own reasoning step (see this skill's
   SKILL.md), not a second LLM call made from inside this script. Use --limit
   / --since to scope down how much the agent needs to classify in one pass.

Usage:
    python ingest/sources/defihacklabs.py --since 2024-01 --limit 50 --out data/defihacklabs_findings.jsonl
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO_URL = "https://github.com/SunWeb3Sec/DeFiHackLabs.git"

# Matches the dominant @KeyInfo convention, e.g.:
#   // @KeyInfo - Total Lost : ~1.7M US$
#   // Attacker : https://etherscan.io/address/0x...
#   // Vulnerable Contract : https://etherscan.io/address/0x...
#   // Attack Tx : https://etherscan.io/tx/0x...
KEYINFO_FIELD_RE = re.compile(
    # The "Total Lost" field sits after "// @KeyInfo - ", not right after "//" like the
    # others - allow (and discard) any text between "//" and the field name, requiring
    # a colon right after the field name so this doesn't grab unrelated comment text.
    r"^//.*?\b(Total Lost|Attacker|Attack Contract|Vulnerable Contract|Attack Tx)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
# Older convention seen on a smaller number of files.
REASON_RE = re.compile(r"//\s*reason\s*:\s*(.+)", re.IGNORECASE)


def clone_or_update(dest: Path) -> Path:
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], capture_output=True)
    else:
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(dest)], check=True, capture_output=True)
    return dest


def parse_header(source: str, max_chars: int = 1500) -> dict:
    head = source[:max_chars]
    fields = {}
    for m in KEYINFO_FIELD_RE.finditer(head):
        key, value = m.group(1).lower().replace(" ", "_"), m.group(2).strip()
        fields[key] = value
    reason_match = REASON_RE.search(head)
    if reason_match:
        fields["reason"] = reason_match.group(1).strip()
    return fields


def protocol_name_from_path(path: Path) -> str:
    name = path.stem  # e.g. "Bedrock_DeFi_exp"
    name = re.sub(r"_exp$", "", name, flags=re.IGNORECASE)
    return name.replace("_", " ")


def date_from_path(path: Path) -> str:
    # Files live under src/test/YYYY-MM/Name_exp.sol
    m = re.search(r"(\d{4}-\d{2})", str(path))
    return m.group(1) if m else ""


def collect_files(repo_dir: Path, since: str | None, limit: int | None) -> list[Path]:
    files = sorted(repo_dir.glob("src/test/*/*_exp.sol"))
    if since:
        files = [f for f in files if date_from_path(f) >= since]
    if limit:
        files = files[:limit]
    return files


def build_record(path: Path, repo_dir: Path) -> dict:
    source = path.read_text(errors="ignore")
    header = parse_header(source)
    protocol_name = protocol_name_from_path(path)
    date = date_from_path(path)

    rec = {
        "source": "defihacklabs",
        "protocol_type": "",       # not stated in the header; left for --classify to fill
        "bug_class": "",           # not stated in the header; left for --classify to fill
        "title": f"{protocol_name} exploit ({date})" if date else f"{protocol_name} exploit",
        "root_cause": header.get("reason", ""),  # often empty - agent fills this in, see SKILL.md
        "pattern": "",
        "preconditions": "",
        "severity": "",            # could be derived from total_lost, but that's deal size, not severity
        "fix": "",
        "protocol_name": protocol_name,
        "poc_source": source,
        "_defihacklabs_meta": {
            "total_lost": header.get("total_lost", ""),
            "attacker": header.get("attacker", ""),
            "vulnerable_contract": header.get("vulnerable_contract", ""),
            "attack_tx": header.get("attack_tx", ""),
            "relative_path": str(path.relative_to(repo_dir)),
        },
    }

    # Classification (bug_class/protocol_type/root_cause/pattern) is NOT done here - this
    # script only parses structural metadata. Filling those fields from the code itself is
    # the calling agent's own reasoning step (see SKILL.md's "Classify a batch" instruction),
    # using whatever model is already running the session - never a second API call from
    # inside this script.
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default="/tmp/DeFiHackLabs")
    ap.add_argument("--since", default=None, help="only files dated YYYY-MM or later, e.g. 2024-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="data/defihacklabs_findings.jsonl")
    args = ap.parse_args()

    repo_dir = clone_or_update(Path(args.repo_dir))
    files = collect_files(repo_dir, args.since, args.limit)
    print(f"Found {len(files)} exploit PoC files" + (f" (since {args.since})" if args.since else ""))

    header_hits = 0
    records = []
    for f in files:
        rec = build_record(f, repo_dir)
        meta = rec["_defihacklabs_meta"]
        if any(meta[k] for k in ("total_lost", "attacker", "vulnerable_contract", "attack_tx")) or rec["root_cause"]:
            header_hits += 1
        records.append(rec)

    print(f"{header_hits}/{len(files)} files had parseable header metadata "
          f"(poc_source kept for all {len(files)} either way; bug_class/protocol_type/root_cause "
          f"are left blank for the calling agent to fill in - see SKILL.md)")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    print(f"Wrote {len(records)} records to {out_path}")
    print(f"Next: python ingest/ingest_findings.py {out_path}")


if __name__ == "__main__":
    main()
