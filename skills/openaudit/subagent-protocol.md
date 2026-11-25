# Resumable Subagent Protocol

Every spawned subagent (Category B pipeline, ledger answering, referee, PoC, report) follows this.
The rule of thumb: **assume the agent dies at any moment; files are the ground truth, the
conversation is a cache.**

## 1. Identity & paths
- Stable id per work unit: `{slug}/{agent}` e.g. `autofinance/kadenzipfel`, `autofinance/ledger/gate-bypass-AbstractRewarder`.
- The agent's report file doubles as its checkpoint: `out/{slug}/reports/{agent}.md` plus, for
  ledger/referee work, the shared structured artifacts (kritt.md rows, candidates.csv rows).

## 2. Start-of-run (resume first, before any work)
1. Read `out/{slug}/task-registry.json`. If a row exists for this id with status `in_progress`,
   you are a RESUME: open your report/checkpoint and continue from the first unfinished item.
2. Re-read the structured state (your kritt.md rows' statuses, your candidates.csv rows, your
   report's progress checklist). Completed units are NEVER redone - re-deriving them wastes the
   API budget and risks contradicting earlier evidence.
3. Append a line to your report: `## RESUME {timestamp} from unit {n}`.

## 3. Incremental write discipline
- Emit results AFTER EACH WORK UNIT (one file, one question row, one family chunk, one finding),
  never batched to the end. Append-only - rewrite is forbidden except status flips.
- Every ledger row flip (UNANSWERED -> ANSWERED/REFUTED) is itself a checkpoint: the ledger IS
  the resume state for answering agents.
- Every candidates.csv add/verdict flip is a checkpoint for referee/Step-6 agents.
- Progress file per agent: `out/{slug}/reports/{agent}.progress.json` =
  `{agent, started_ts, updated_ts, last_unit, units:[{key,status}], status}`. Update it in the
  same append that writes a unit - it is the cheap "where am I" a fresh agent reads FIRST.

## 4. Idempotency
- Units have stable keys (question row-id, function path:line, candidate id). On resume, grep for
  the key before redoing it. No duplicate rows, no double-counted work.

## 5. Parent (opencode session) bookkeeping
- On spawn, parent records `{id, task_id, status: in_progress, checkpoint: "<last unit>", ts}`
  in task-registry.json; on completion flips to `done` (or `failed` + reason).
- API/network hiccup (same opencode session): re-invoke the SAME subagent with its saved
  `task_id` - conversation context persists in-session, so continue works cleanly.
- Process death / new opencode session: spawn a FRESH agent with the resume directive in §2 -
  files carry the state; task_ids do not cross restarts.

## 6. Long-running stages
- fuzz campaigns: the corpus DB + violations/ dir are the checkpoint; a killed run restarts from
  the corpus (that is how echidna/medusa are designed to work).
- slither/semgrep/etc.: write output JSON to disk as soon as done; never hold results in chat.

## 7. Budget note for your hardware
This protocol exists because the failure modes are yours: flaky network mid-agent-call, laptop
battery death, API rate limits. The cost of following it is a few extra appends; the saving is
never re-paying for lost work.

## 8. Handoff & deviation discipline
- **Read the handoff before work:** `out/{slug}/handoff/{submitted-findings.md, known-issues.md,
  prior-audits/}` is in your run-context. It exists so the run never re-submits a bug the operator
  already sent, a program-accepted risk, or a prior-audit finding.
- **Dedup before promotion:** before flipping a candidate to CONFIRMED / submission-grade
  (a `submit` candidates.csv row or a `referee_verdict` flip), check it against the handoff. On a
  match, set `dup_signature` to the matched id, log `dup-of:<id>` in
  `out/{slug}/reports/{pipeline}-kritt-deviations.md`, and DO NOT promote.
- **Tier M disagreement = deviation, not edit:** later pipelines never modify `kb/kritt.md`; a
  disagreement is appended to `out/{slug}/reports/{pipeline}-kritt-deviations.md`.
- **Blind-discovery guard:** only operator-SUBMITTED findings, known issues, and prior audits are
  shared across agents. Never pass one pipeline another pipeline's un-promoted candidate list -
  unsubmitted peer work stays private to preserve independent discovery.
