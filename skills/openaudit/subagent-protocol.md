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
