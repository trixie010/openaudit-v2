---
name: target-intel
description: Score a bug-bounty or audit target from a pasted bounty program page. Extracts payout tiers, TVL, payable impacts, KYC/response terms, chains, assets, and competition signals, then emits a transparent EV score plus red flags to out/{slug}/program-intel.{md,json}. Use at target intake, before spending audit budget, when the operator pastes a program page.
---

# target-intel

The operator usually pastes the whole bounty page into chat. This turns that
paste into a structured, auditable **go/no-go score** before any pipeline budget
is spent. It is a heuristic decision aid, not a promise - the operator decides.

## Why this exists

Top hunters don't just find bugs better; they pick **winnable** programs (payout,
competition, payable impacts) and **keep duplicate submissions near zero**. This
tool makes the "is this program worth my hours?" decision explicit and repeatable,
and it seeds the impact table the referee will use later.

## Workflow

```bash
# save the pasted page (or pipe it)
python skills/target-intel/scripts/program_intel.py --slug myproto --text out/myproto/PROGRAM.md
pbpaste | python skills/target-intel/scripts/program_intel.py --slug myproto --stdin

# optional TVL enrichment + competition overrides
python .../program_intel.py --slug myproto --text PROGRAM.md \
  --defillama aave --participants 120 --freshness new
```

Writes `out/{slug}/program-intel.md` and `program-intel.json`, and prints a short summary.

## What it extracts

- **Payout table** per severity (Critical/High/Medium/Low) + max bounty.
- **TVL** (from the page, or DefiLlama via `--defillama`).
- **Payable impacts** (in-scope bullets) and **out-of-scope / known issues**.
- **KYC** requirement, **response time**, **chains**, contract addresses, GitHub repos.
- **Competition** signals (participants / submissions).

## Score (0-100, higher = better expected value)

| Component | Max | Basis |
|---|---|---|
| Payout | 30 | log-scaled max bounty to $1M |
| TVL | 20 | log-scaled to $1B (more value = more severe bugs) |
| Payable impacts | 15 | count of theft/freeze/drain/insolvency/governance etc. |
| Surface | 5 | addresses + repos |
| Response speed | 5 | <=24h full, <=72h partial |
| Freshness | 5 | `--freshness new/recent/old` (default neutral) |
| Competition | -10 | participants/submissions (crowded = lower EV) |
| KYC | -5 | friction |

Tier: **A** >= 70, **B** 50-69, **C** < 50.

## How to use the output

1. **Seed the impact table**: copy the extracted payout table verbatim into
   `SCOPE.md` and `kb/impact-ruleset.md` (the referee quotes it verbatim).
2. **Red flags**: missing TVL/payout/competition, KYC, "Low/Info out of scope",
   "domain X accepts ONLY Critical" - check these before committing hours.
3. **Go/no-go**: Tier C or heavy red flags -> prefer a different program; the
   scorer is transparent so you can override (e.g. known-quiet program).

## Limits

- Free-text parsing is heuristic; verify every number against the page.
- Competition is often not on the page - pass `--participants`/`--submissions`
  or don't trust that term.
- Freshness is operator-supplied (`--freshness`), not inferred.
