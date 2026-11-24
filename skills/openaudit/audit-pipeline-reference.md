# Audit Pipeline Reference

This file describes how an AI agent should run each audit skill pipeline.
Each pipeline is a combination of tools (Slither, Aderyn, Semgrep, Wake detectors),
AI-driven methodology skills (structured markdown prompts the agent follows),
and the Category C fuzzing executor (Recon Magic + Chimera + Medusa + Echidna).

## Pipeline Categories

### Category A: Static Analysis Tools (automated)

These pipelines run external binaries on the source code. They produce machine-readable output
that the agent parses and includes in the report.

| Pipeline | Tool | Command | Output |
|----------|------|---------|--------|
| Slither | Python static analyzer | `uv run slither {src_dir} --json {output}.json` | JSON with detectors, severity, locations |
| Aderyn | Rust static analyzer | `aderyn {src_dir} --output {output}.md` | Markdown report with H/L findings |
| Semgrep | Pattern matching | `uv run semgrep --metrics=off --config "r/solidity" --json {src_dir}/ > {output}.json` | JSON with rule matches |
| Wake detectors | Ackee Python static analyzers | `uvx wake-exec detect {src_dir}` | independent detector signal (different authors than Slither); do NOT adopt Wake's compile/test stack |

**Important**: The source directory must be a git repository for Semgrep to scan files.
Run `git init && git add -A && git commit -m init` in the source dir if needed.

**Solc version**: Match the compiler version used by the contract (check `pragma solidity` lines).
Install with: `uv run solc-select install {version} && uv run solc-select use {version}`

### Category B: AI-Driven Methodology Skills (agent reads and follows)

These are structured markdown prompts. The agent reads the skill file and follows
the methodology step by step against the downloaded source code. **No external tools needed** —
the agent performs the analysis using its own reasoning over the code.
Every Category B agent additionally carries the KRITT ledger duty (SKILL Step 5): tier S rows
answered per pipeline, tier M cleared once per mechanism family, role-gate refutations only via
the 3-part bypass sub-check - and all output via the canonical finding schema + task-registry
resumability (subagent-protocol.md).

Each skill targets different vulnerability classes and uses different techniques.
Running all of them provides defense-in-depth: what one misses, another catches.

### Category C: Fuzzing Executor (Recon Magic + Chimera + Medusa + Echidna)

See [fuzzing-pipeline.md](./fuzzing-pipeline.md) for the full operating procedure. Runs as a
PARALLEL track to A and B (never gates them, never gated by them) on the night shift. Feeds the
shared `out/{protocol_slug}/candidates.csv`; violations become Foundry PoC skeletons via
`runes` (Enigma-Dark) and then face the Step 5.5 referee like everything else.

---

## Solidity-Applicable Pipelines (run all of these for EVM audits)

### Pipeline 0: Knowledge Base (Stage 0.5 - runs BEFORE all others)
- **Repo**: built-in stage; graph layer from deps/chainscope, manifest from slither Python API
- **Type**: prerequisite for Categories B and C
- **What it produces** (10 artifacts, out/{slug}/kb/): MECHANICAL: functions.csv (canonical,
  compiler-grounded manifest), graph-functions.csv (parser layer), chainscope.db (queryable
  graph), slither-manifest.json (raw slither run; detectors also feed Category A),
  dependency-map.md (per-function reads/writes/calls from the graph), reconciliation.md
  (compiler-vs-parser diff; parser gaps flagged). AGENT-GENERATED: function-docs.md,
  access-control.md (role matrix), invariants.md (properties tagged with recon-magic classes:
  PROFIT/CANARY/WTOK/ACCUM/STATE_MACHINE/FEE_VOL/DOOMSDAY/NEGATIVE), kritt.md (KRITT question
  ledger, two tiers via templates/kb_kritt.py: TIER M mechanical family-aggregated rows from the
  graphs - slot-writer, external-call, sink, gate-bypass, parser-gap, floor-not-cap; TIER S
  semantic rows from invariants.md - graph-invisible, per-pipeline, where Highs live. 30+ is a
  FLOOR; the run is not done while rows stay UNANSWERED; role-gate refutations require the
  3-part bypass sub-check)
- **Why**: anti-skimming. Every later agent cites KB entries instead of re-reading raw code, and
  every report must end with BOTH a per-function coverage matrix checked against functions.csv AND
  a kritt question ledger where every question is ANSWERED / REFUTED (with blocking line) /
  UNANSWERED (re-queued) - completion is gated on the ledger, not on "feeling done".
- **Structural graph layer - ChainScope** (`deps/chainscope`): `cs_profile.py {src} --strategy
  bounty --json`, then cs_paths/cs_sinks/cs_state/cs_trace/cs_cross/cs_summary answer: riskiest
  functions, who writes balance slots, deposit->delegatecall paths, trust-boundary crossings,
  production-vs-research provenance. Outputs: priority order for fuzz target-functions.json +
  property targets for the KB (its own README is honest: "Map, not researcher").

### Pipeline 1: Slither (static analysis)
- **Repo**: Built-in (pyproject.toml dependency)
- **Type**: Category A — automated tool
- **What it finds**: Reentrancy, uninitialized variables, shadowing, unchecked calls, compiler bugs
- **Run**: `uv run slither {src_dir} --json {output_dir}/slither-output.json`
- **Parse**: Group findings by impact (High/Medium/Low/Informational), filter out Informational

### Pipeline 2: Aderyn (static analysis)
- **Repo**: Installed via `cargo install aderyn`
- **Type**: Category A — automated tool
- **What it finds**: Unsafe casting, unprotected initializers, centralization risks, locked ether
- **Run**: `aderyn {src_dir} --output {output_dir}/aderyn-report.md`
- **Note**: May panic on non-standard version strings — the report is still generated before the panic

### Pipeline 3: Semgrep (pattern matching)
- **Repo**: Built-in (pyproject.toml dependency)
- **Type**: Category A — automated tool
- **What it finds**: Gas optimizations, unsafe patterns, coding style issues
- **Run**: `uv run semgrep --metrics=off --config "r/solidity" --json {src_dir}/ > {output_dir}/semgrep-results.json`
- **Important**: Always use `--metrics=off` to prevent telemetry

### Pipeline 4: Pashov Solidity Auditor (AI methodology, v4.1)
- **Repo**: `deps/pashov-skills/`
- **Type**: Category B — AI-driven methodology
- **What it finds**: Business logic bugs, attack vectors across 12 specialist agent lenses
- **Skill file**: `deps/pashov-skills/solidity-auditor/SKILL.md`
- **How to run**:
  1. Read `deps/pashov-skills/solidity-auditor/SKILL.md` completely (462 lines; it is an
     orchestrator contract, not a checklist)
  2. Discover in-scope files with the exact `find` command the skill mandates — use Bash `find`,
     with `-type f` (Hardhat writes build artifacts into directories named `Contract.sol/`)
  3. Discover the agent roster from `deps/pashov-skills/solidity-auditor/references/hacking-agents/`
     — 12 specialist agents (`access-control`, `asymmetry`, `boundary`, `economic-security`,
     `execution-trace`, `first-principles`, `flow-gap`, `invariant`, `math-precision`,
     `numerical-gap`, `periphery`, `trust-gap`) plus `shared-rules.md`
  4. Read the shared prep set in one batch: `references/agent-prompts.md`, `references/judging.md`
     (confidence threshold `75`), `references/report-formatting.md`, `references/report-language.md`
  5. Spawn the 12 agents in parallel, one agent per hacking-agents file, each armed with
     `shared-rules.md`. If parallel agents are unavailable, run them sequentially per file
  6. For loop mode (`--loop [N]`, default 3): each pass is told what earlier passes found; runs
     write `run-{n}.md` markers, dedupe + gate-evaluate per pass (`references/dedup-and-assembly.md`)
  7. **The report is assembled by shell, not by the model.** Turn 5 runs
     `deps/pashov-skills/solidity-auditor/references/assemble.sh --dir .solidity-auditor/runs/{stamp}`
     which writes `{dir}/full-report.md`. Do NOT compose or reword a report yourself — the
     assembler is the only report producer and its structure check surfaces unreadable blocks
  8. Read the assembled `full-report.md` and summarise it into `{output_dir}/pashov-skills.md`,
     preserving the assembler's confidence scores, `seen in k/N runs`, and `NEW`/`KNOWN` memory tags
- **In-scope files**: All `.sol` files EXCEPT dependency/build dirs (`node_modules/`, `lib/`,
  `artifacts/`, `cache/`, `out/`, `broadcast/`, `coverage/`, `typechain*/`) and non-production
  (`interfaces/`, `mocks/`, `test/`, `*.t.sol`, `*Test*.sol`, `*Mock*.sol`). **Deploy scripts
  stay in scope** (`script/`, `deploy/`, `*.s.sol`) — they set constructor args and hand over
  ownership, so they carry real bugs. An explicitly named file on the command line is always
  scanned regardless of the exclude pattern
- **Output**: Assembled at `.solidity-auditor/runs/{stamp}/full-report.md`; summarised to `{output_dir}/pashov-skills.md`
- **Note**: v4 replaced the old `references/attack-vectors/attack-vectors-{1,2,3,4}.md` scheme
  with the 12 `hacking-agents/` files and dropped the mandatory marker protocol in favour of
  shell assembly. Follow the current `SKILL.md`, not earlier descriptions of 4 attack-vector files

### Pipeline 5: SCV-Scan / kadenzipfel (AI methodology)
- **Repo**: `deps/kadenzipfel-scv-scan/`
- **Type**: Category B — AI-driven methodology
- **What it finds**: 36 specific vulnerability types from a curated database
- **Skill file**: `deps/kadenzipfel-scv-scan/SKILL.md`
- **How to run**:
  1. Read `deps/kadenzipfel-scv-scan/SKILL.md`
  2. **Phase 1**: Read `deps/kadenzipfel-scv-scan/references/CHEATSHEET.md` — memorize all 36 patterns
  3. **Phase 2a**: Grep-scan the source code for trigger patterns from the cheatsheet
  4. **Phase 2b**: Semantic read-through of source code for logic patterns
  5. **Phase 3**: For each candidate, read the full reference file (e.g. `references/reentrancy.md`)
     and walk through Detection Heuristics and False Positive conditions
  6. **Phase 4**: Report confirmed findings only
- **Output**: Write to `{output_dir}/kadenzipfel-scv-scan.md`

### Pipeline 6: Forefy Smart Contract Audit (AI methodology)
- **Repo**: `deps/forefy-context/`
- **Type**: Category B — AI-driven methodology
- **What it finds**: Protocol-specific vulnerabilities, cross-contract issues, economic attacks
- **Skill file**: `deps/forefy-context/skills/blockchain/smart-contract-audit/SKILL.md`
- **How to run**:
  1. Read `deps/forefy-context/skills/blockchain/smart-contract-audit/SKILL.md`
  2. Detect protocol type (DEX, lending, pool, governance, etc.)
  3. Read language-specific checks from `deps/forefy-context/skills/blockchain/smart-contract-audit/solidity-checks.md`
  4. Read vulnerability patterns from `deps/forefy-context/skills/blockchain/smart-contract-audit/reference/solidity/`
  5. Follow the 5 audit layers: protocol → economic → access control → integration → technical
  6. Apply the multi-expert framework from `deps/forefy-context/skills/blockchain/smart-contract-audit/multi-expert.md`
- **Output**: Write to `{output_dir}/forefy-context.md`

### Pipeline 7: QuillAI Behavioral State Analysis (AI methodology)
- **Repo**: `deps/quillai-qs-skills/`
- **Type**: Category B — AI-driven methodology
- **What it finds**: State invariant violations, economic exploits, access control gaps
- **Skill file**: `deps/quillai-qs-skills/plugins/behavioral-state-analysis/skills/behavioral-state-analysis/SKILL.md`
- **How to run**:
  1. Read the SKILL.md above
  2. Classify the contract type (DEX, lending, vault/pool, utility, governance, etc.)
  3. Select relevant threat engines based on contract type
  4. **Phase 1**: Behavioral decomposition — extract states, invariants, roles, value flows
  5. **Phase 2**: Threat modeling with Economic, Access Control, State Integrity engines
  6. **Phase 3**: Exploit verification — build attack sequences for Critical/High
  7. **Phase 4**: Confidence scoring
- **Additional QuillAI plugins to run** (each has its own SKILL.md in `plugins/{name}/skills/{name}/`):
  - `reentrancy-pattern-analysis` — all reentrancy variants
  - `oracle-flashloan-analysis` — price manipulation, flash loans (if protocol uses oracles)
  - `proxy-upgrade-safety` — storage collisions, function clashing (if proxy pattern detected)
  - `external-call-safety` — unsafe calls, fee-on-transfer tokens
- **Output**: Write to `{output_dir}/quillai-qs-skills.md`

### Pipeline 8: Auditmos DeFi Checklists (AI methodology)
- **Repo**: `deps/auditmos-skills/`
- **Type**: Category B — AI-driven methodology
- **What it finds**: DeFi-specific vulnerabilities across 14 categories
- **How to run**:
  1. Read `deps/auditmos-skills/skills/always-checklist.md` — the master checklist
  2. Based on the protocol type, read the relevant domain-specific skill:
     - Pool/vault → `audit-staking/`, `audit-slippage/`
     - Lending → `audit-lending/`, `audit-liquidation/`, `audit-liquidation-calculation/`
     - Oracle-dependent → `audit-oracle/`
     - Has signatures → `audit-signature/`
     - Math-heavy → `audit-math-precision/`
     - General → `audit-reentrancy/`, `audit-state-validation/`
  3. For each applicable skill, read its checklist.md and systematically verify each item
- **Output**: Write to `{output_dir}/auditmos-skills.md`

### Pipeline 9: Trail of Bits Skills (AI + tools)
- **Repo**: `deps/trailofbits-skills/`
- **Type**: Category A+B — mixed
- **What it finds**: Entry point analysis, specification compliance, architecture review
- **How to run**:
  1. Read `deps/trailofbits-skills/CLAUDE.md` for orientation
  2. **Entry point analysis**: Read `deps/trailofbits-skills/plugins/entry-point-analyzer/skills/entry-point-analyzer/SKILL.md`
     and identify all state-changing entry points in the contract
  3. **Spec-to-code compliance**: Read `deps/trailofbits-skills/plugins/spec-to-code-compliance/skills/spec-to-code-compliance/SKILL.md`
     and check if the implementation matches documented specifications
  4. **Audit context building**: Read `deps/trailofbits-skills/plugins/audit-context-building/skills/audit-context-building/SKILL.md`
     to build architectural context
  5. If CodeQL is installed, run CodeQL queries from the static-analysis plugin
- **Output**: Write to `{output_dir}/trailofbits-skills.md`

### Pipeline 10: Archethect SC Auditor (AI methodology + MCP tools)
- **Repo**: `deps/archethect-sc-auditor/`
- **Type**: Category A+B — mixed
- **Skill file**: `deps/archethect-sc-auditor/skills/security-auditor/SKILL.md`
- **How to run**:
  1. Read the SKILL.md above for the Map-Hunt-Attack methodology
  2. **Setup**: Review scope and codebase structure
  3. **Map**: Build architecture map (contracts, inheritance, external calls, storage)
  4. **Hunt**: Identify vulnerabilities using the methodology
  5. **Attack**: Develop proof-of-concept exploits for critical findings
  6. If MCP tools are configured, use them to run Slither/Aderyn/search Solodit
- **Note**: The MCP tools component requires `npm install` and Node.js setup. The methodology skill
  works standalone without MCP.
- **Output**: Write to `{output_dir}/archethect-sc-auditor.md`

### Pipeline 10.5: Disclosed Findings Corpus (Category B, retrieval-grounded)
- **Skill**: `skills/disclosed-findings-corpus/SKILL.md` (local addition, not a `deps/` submodule)
- **Type**: Category B — AI methodology, grounded by retrieval over past disclosed findings
- **What it finds**: recurrent, well-documented DeFi bug classes (oracle manipulation, share
  inflation, reentrancy, signature replay, rounding/precision, access control) whose *shape*
  appears in the target — the classes greedy per-file scanners miss and that poor recall causes
  weak models to overlook
- **Prereq**: corpus DB built once (`data/findings.db`); see that skill's SKILL.md "Setup".
  Retrieval is SQLite FTS5 (stdlib) — no sklearn/numpy/embeddings needed
- **How to run** (per in-scope file/function, same scope as other Category B pipelines):
  1. `python scripts/retrieval/query.py "<what this code does>" --protocol-type <lending|amm|vault|staking|bridge> --top 3`
     (run from `skills/disclosed-findings-corpus/`)
  2. Treat each returned pattern as a hint to check against THIS target's code
  3. **Hard rule — a retrieved pattern is never itself a finding.** Report ANSWERED only if you
     can cite the exact line(s) in THIS target's code that cause the failure; restating the
     retrieved pattern with no target line to point at is REFUTED/UNANSWERED
  4. Write findings to `out/{protocol_slug}/reports/disclosed-findings-corpus.md`, and record
     which retrieved finding (title + source) grounded each one
- **Dedup use is separate** (Step 7 / confirm time): `python scripts/retrieval/query.py "<finding>" --dedup --protocol-name "<target>"`
  is exact-match scoped to the same protocol — never call it during Step 5 discovery (blind rule)
- **Why alongside the blind pipelines, not instead**: it is an ADDITIONAL vote, same tier as
  pashov/kadenzipfel/forefy — let Step 5.5's referee weigh it with the rest
- **Output**: `{output_dir}/disclosed-findings-corpus.md`

### Pipeline 11: Recon Magic Fuzzing Executor (Category C)
- **Repo**: `deps/recon-magic-framework` (Python workflow engine, 75 phase-prompt files)
- **Type**: executable pipeline - runs Chimera harnesses under Medusa AND Echidna
- **What it finds**: violations of the generated property tiers - attacker profit, fee-volume
  conservation ghosts, accumulation/rounding drift, state-machine illegal transitions, weird-token
  behaviors (WTOK), reentrancy via callback mocks, negative/DOOMSDAY properties
- **Run**: per [fuzzing-pipeline.md](./fuzzing-pipeline.md) - throwaway clone, scope gate via
  target-functions.json (from kb/functions.csv), chimera-test-linter PRE-FLIGHT MANDATORY,
  short workflows only (budget), night shift; violations -> `runes` -> Foundry PoC skeleton -> Step 5.5 referee
- **Notes**: its own findings have NO independent refutation stage - that is openaudit's job here.
  Never run its audit-naive / first-audit workflows (superseded by Category B).

### Pipeline 12: Referee (Step 5.5 - runs AFTER each pipeline batch)
- **Type**: fresh adversarial agent per batch; refutation-only mandate
- **Jobs**: re-litigate every "Dropped candidates" entry (return candidates whose blocking citation
  fails); apply FP-PREVENTION patterns FP-1..FP-7 before accepting any dismissal; verdict every
  candidate CONFIRMED/BLOCKED/LEAD (LEADs become Category C properties); diff coverage matrices
  against kb/functions.csv and re-queue unaccounted functions; escalation rule: cross-category
  disagreement keeps the higher severity until a PoC settles it. ALSO audits the kb/kritt.md
  ledger (zero UNANSWERED rows; role-gate REFUTED rows need the 3-part bypass; any
  ROUTINE-OPERATION row refuted on 'only privileged callers' = automatic overturn) and applies
  the composed ranker when re-grading. Two sight-lines (SKILL 5.5): the ledger-grader SEES the
  finder's citations; the §8 execution skeptic runs BLIND to the finder on `confirmed-*` rows,
  upholding or marking `disputed` (ladder: suspected -> confirmed-executable ->
  confirmed-differential -> reproduced). Resume-safe via task-registry.json (subagent-protocol).

## Non-Solidity Pipelines (skip for EVM audits)

| Pipeline | Language | When to use |
|----------|----------|-------------|
| `frankcastle-safe-solana` | Rust (Solana Anchor) | Solana program audits only |
| `membrane-core` | Rust (CosmWasm) | CosmWasm protocol reference only |

## Important: Library and Extension Coverage

When auditing a contract, do NOT limit analysis to just the main contract file.
The audit MUST cover:

1. **All libraries** imported by the contract (SafeTransferLib, EnumerableSet, etc.)
2. **All inherited contracts** in the mixin/diamond hierarchy
3. **Extension contracts** that are called via delegatecall or staticcall from fallback functions
4. **External contracts** at addresses stored in state (governance, extension registries, routers, hooks, etc.)

For libraries and inherited contracts, include them in the source code scope for all pipelines.
For extension/external contracts, download their source code separately (using Sourcify/Etherscan)
and note in the report which external contracts were and were not analyzed.
