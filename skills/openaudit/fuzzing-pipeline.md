# Category C Fuzzing Pipeline - Operating Procedure

Tools: Recon Magic Framework (deps/recon-magic-framework) orchestrating Chimera harnesses run
under BOTH Medusa and Echidna. This pipeline is the night shift. It is a PARALLEL track: it never
waits for Category B and nothing waits for it; both feed `out/{protocol_slug}/candidates.csv`.

## WHEN THIS PIPELINE RUNS - pipeline modes (SKILL.md Step 5)

- `--mode audit`: Category C is SKIPPED ENTIRELY. Contest-speed audits do not run harnesses.
- `--mode bounty`: this pipeline runs as night campaigns on the public fuzz-lab repo.
- `--mode full`: this pipeline + fork-mode + Step 6.5 evmbench exploitation + property iterations.

Fuzzing must never slow down an audit contest: in audit mode do not build harnesses, do not run
echidna/medusa, and do not burn API calls on workflow setup. The KB invariants from Stage 0.5 are
still written (they feed submissions directly), but harness work waits for bounty/full mode.

## Hardware/network model

- **Execution options (2026-09-11):** laptop + tmux works today; the PUBLIC fuzz-lab repo
  (github.com/trixie010/fuzz-lab, `fuzz-night.yml`) runs 6h campaigns on GitHub Actions with
  unlimited minutes - preferred for nightly coverage once `targets/` holds a real campaign
  (schedule stays disabled until then - an empty targets dir must not fail-email the operator);
  Oracle VPS deferred (signup pending a
  working card). All three feed the same candidates.csv via the referee.
- **Runtime prerequisites:** medusa 1.5.1 + echidna 2.3.3 as global binaries in `~/.local/bin`
  (release tarballs, not source builds). forge/cast present. No per-project install needed -
  any workspace uses the global binaries.
- LLM-heavy phases (scout, setup, property generation): need internet + API calls. Run while the
  operator is present.
- Fuzz execution (echidna/medusa/chimera): LOCAL CPU only. Runs unattended, wifi may be off.
- If a connection drop kills an agent phase mid-run: state lives on disk (`magic/`, `test/recon/`);
  workflows are phase-addressed (skip/jump/gates) - restart the FAILED PHASE, not the run.
- Low-RAM hosts: one workflow at a time, `--workers 2` for medusa, `workers=1` in echidna.yaml.
- **Overnight survival rules** (user's laptop pattern: dies often, battery + flaky net):
  - ALWAYS launch long fuzz runs inside `tmux` (or `screen`): closing the terminal/SSH session
    must not kill the fuzzer. `tmux new -s fuzz 'medusa ...'`, detach with Ctrl-b d, resume with
    `tmux attach -t fuzz`.
  - Lid close on Windows defaults to SLEEP: that suspends WSL (fuzzer pauses, NOT killed -
    corpus persists, medusa resumes coverage from it on next launch). To keep it actually running
    with the lid closed: Windows power settings -> "When I close the lid" -> "Do nothing"
    (plugged-in at minimum). Locking the screen does NOT stop processes; only sleep does.
  - Battery death mid-run is ACCEPTABLE: both fuzzers persist their corpus database continuously;
    next launch re-seeds from it and continues deepening coverage. Never restart from zero unless
    the corpus dir was deleted.

## Per-target setup (ALWAYS a throwaway clone - recon-magic writes into the repo)

Use the scaffolding template: `templates/init_fuzz_workspace.sh {repo_url} {protocol_slug}`
with `templates/target-functions.sample.json` and `templates/candidates.sample.csv` as formats.

    SRC={output_dir}/fuzzing/target-work/
    rm -rf $SRC && git clone --depth 1 {repo_url} $SRC && cd $SRC
    ln -s {repo_root}/deps/recon-magic-framework ./rmf   # or copy prompts/ into place
    mkdir -p magic

### Campaign directory layout (modeled on perimetersec/drips-fuzzing, origin-oeth-fuzzing)

    {output_dir}/fuzzing/
      target-work/          # throwaway clone (harness + magic/ live inside)
      SCOPE.md              # copied from out/{slug}/SCOPE.md - fuzz scope = audit scope
      target-functions.json # THE SCOPE GATE: only these functions get handlers/fuzzed
      coverage/             # lcov + covg-scoring notes per run
      violations/           # raw echidna/medusa reproducer outputs
      candidates-added.log  # what this track appended to candidates.csv (audit trail)

## Procedure (day: thinking; night: grinding)

1. **Scope gate first**: write `target-functions.json` from kb/functions.csv rows with `in_scope=true` (generator consumed in-scope-files.txt) - the in-scope
   state-changing functions only. Recon-magic coverage/properties phases are instructed to touch
   NOTHING outside this list (their own enforcement: "Only analyze functions explicitly listed").
2. **KB feed**: hand recon-magic the out/{slug}/kb/dependency-map.md + invariants.md. Its
   properties phases (deps/recon-magic-framework/prompts/agent/properties-v4-*.md) turn these into
   property code. Property classes available: PROFIT (attacker-gain via ProfitTracker), CANARY,
   WTOK (weird tokens - use perimetersec/fuzzlib behaviors when recon mocks are thin), ACCUM
   (accumulation ghost tracking), COMPLY, STATE_MACHINE, FEE_VOL (ghost), DOOMSDAY, NEGATIVE,
   plus flash-loan attacker + reentrant callback handlers.
3. **Harness build** (scout-v2 + setup-v2 phases): Setup.sol, AdminTargets/ActorTargets split,
   prerequisite handlers, validation unit tests.
4. **PRE-FLIGHT GATE - mandatory**: run the chimera-test-linter checklist (deps/recon-magic-framework
   prompts/agent/chimera-test-linter.md) over the generated harness before ANY long run. A harness
   that silently skips the target (missing `vm.prank(_getActor())` before the target call, wrong
   modifiers, clamped-handler misuse) reports "clean" forever - this gate exists to prevent
   pretending to fuzz.
5. **Night grind**: `workflow-fuzzing-coverage-short` or echidna/medusa workflows directly; cap
   loops; log coverage snapshots. Objective: coverage of every target-functions.json entry;
   prioritize by covg-scoring (depth of uncovered lines), not just breadth.
   Standalone medusa quickstart (no recon-magic workflow): `medusa init` in the target project
   (auto-detects foundry), then patch medusa.json: workers, timeLimit, targetContracts=[harness];
   run `medusa --config medusa.json` (inside tmux).
6. **Ingest next morning**: every violation -> convert to a Foundry PoC SKELETON - optionally via
   `runes` (Enigma-Dark/runes, small Go CLI, optional install) or manually - then verify by hand
   -> only then append to candidates.csv with poc_status=skeleton. The Step 5.5 referee owns
   promotion from there - and the composed ranker (`templates/impact-ranker.md` + program table)
   applies to fuzz candidates exactly as to AI findings. Fuzz violations are also tier-S
   answers: a kritt.md row flipped to ANSWERED citing a violation is the strongest evidence kind;
   a raw fuzzer trace enters the evidence ladder only as `suspected` - `confirmed-executable`
   requires a real runner (converted-verified PoC) passing, never the trace alone
   the ledger knows.
7. **LEAD promotion loop**: referee verdict LEAD = write the violated property from the lead and
   add it to the harness. Category B leads feed Category C; Category C violations feed the referee.
   The loop, not either half, is the product.

## Exploit verification (Step 6.5) - evmbench-style live exploitation

MODE-INDEPENDENT: this stage needs NO fuzzing and runs in ALL pipeline modes (audit/bounty/full)
for High/Critical findings that reached referee-CONFIRMED with a passing PoC. Cost: minutes per
finding; anvil fork ~1GB RAM; exploit runs are seconds. It is evidence-hardening for submissions,
not discovery.

For High/Critical submissions, a passing unit-test PoC is not the ceiling: reproduce the exploit
against a LIVE local chain (anvil fork of mainnet state) and show funds actually move.

- Tool: `evmbench` scripts from alt-research2/SolidityGuard (user decision 2026-09-10: use their
  code directly; proprietary upstream -> NEVER in a public repo/fork, and NOT vendored into this
  repo's git tree - user clones SolidityGuard into their workspace and points our pipeline at it)
- What it gives: detect/exploit/patch prompt modes; exploit mode = agent gets an Anvil chain
  (fork, chain ID 31337), funded exploit wallet, Foundry cast/forge + ethers; transactions are
  auto-captured from chain state (no manual txs.json); RPC filtered via a Veto proxy
- Our flow: 1) referee CONFIRMED finding (any `confirmed-*` ladder status) -> 2) build exploit tx
  sequence on anvil fork of the actual deployed protocol state -> 3) capture before/after balances
  -> 4) attach the captured tx list + balance deltas to the submission as exploit evidence,
  flipping the finding's ladder `status` to `reproduced` and updating
  `evidence_boundary`/`submission_advice` on its candidates row
- License note: SolidityGuard is proprietary - the repo must stay out of openaudit's git tree and
  out of any public push. CLI usage of a local clone is the user's accepted risk.
- Machine migration note: SolidityGuard is NOT part of the openaudit repo (proprietary). On a new
  machine, re-clone it manually before Step 6.5 works:
  `git clone --depth 1 https://github.com/alt-research2/SolidityGuard ~/tools/SolidityGuard`
- Laptop cost: anvil fork is cheap (~1GB); exploit runs are seconds. This stage does NOT need the
  night shift.

## Budget guards (free-tier reality)

- ONE workflow per session; prefer *-short variants; set max loops/decisions where configurable
- Agent phases cost API calls; fuzz phases cost electricity only - front-load LLM work in one
  sitting, then let the night be free
- Record every run in candidates-added.log with workflow name + phases executed (Nudge System can
  inject the openaudit SCOPE rules into every recon-magic agent call without editing 75 prompt files:
  add a top-level "nudge" field in the workflow JSON)

## Validation test for the pipeline itself

Before pointing this at fresh bounty targets, run it on a codebase with KNOWN planted answers:
e.g. Tokemak v2-core-pub rewarders - PASS condition = the night grind rediscovers
queueNewRewards dust div/0 (P-01) and sub-MIN-STAKE stranding (F-01) from generated properties,
WITHOUT hand-written tests. If it passes, the stack is real; if not, the failure localizes to a
component (manifest, harness, linter, or fuzzers) and gets fixed before any time is spent on it.

## Known framework weaknesses (documented, accepted)

- Young project (26 stars): its findings are LEADS until verified - same rule as all AI output here
- It has no independent refutation stage; this repo's Step 5.5 referee supplies that
- audit-naive / first-audit workflows NOT used (superseded by Category B)
- Its LLM decision points can mis-branch; gates/loops are capped and logged above
