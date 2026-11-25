---
name: eval-harness
description: Run openaudit BLIND against past Code4rena contests with known High/Medium findings and score recall, false positives, and duplicate submissions. Use when measuring audit-pipeline quality, regression-testing prompts, or tuning for fewer duplicates and higher unique-finding recall. Ground truth = code-423n4/<contest>-findings (report.md + data/*.json); produced findings = out/<slug>/candidates.csv.
---

# eval-harness

A meta-tool, **not** an audit pipeline. It answers "is openaudit actually finding
the real bugs, and how many junk/duplicate submissions is it producing?" by
replaying past contests where the answer key is public.

It is deliberately **outside** the Category B roster and must never run during a
live audit - it would leak the answer key into a blind run.

## Why Code4rena

Every C4 contest ends with a `code-423n4/<YYYY-MM>-<name>-findings` repo that ships:

- `report.md` - the official, **deduped** report: `## [[H-01]/[M-01]] Title` sections
  with Impact / Proof of Concept / Mitigation, and YAML front-matter (`slug`,
  `title`, `date`, `contest`). This is the answer key.
- `data/<warden>-<issue>.json` - every individual submission: `{risk, title,
  issueId, originalIssueId, ...}` where `risk` is `3`=High, `2`=Medium, `Q`=QA.
  `originalIssueId` maps duplicates to one root, so we can measure duplicate rate.

No API key, no live code4rena.com - repos are on GitHub and fetched with `git`.

## Produced artifact

openaudit writes `out/<slug>/candidates.csv` (canonical schema from
`skills/openaudit/templates/candidates.sample.csv`). The harness scores that,
falling back to the final report only if the CSV is absent.

## Workflow

```bash
# 1. Fetch answer key + target (reuse local clones with --findings-src / --target-src)
python scripts/evalctl.py fetch 2024-07-reserve --workdir ~/openaudit/eval-runs/2024-07-reserve

# 2. Print the BLIND brief and run openaudit on target/ in a SEPARATE session.
#    IMPORTANT: run with the answer key out of sight (see "Blindness" below).
python scripts/evalctl.py blind 2024-07-reserve --workdir ~/openaudit/eval-runs/2024-07-reserve
#    ... operator runs /openaudit against target/, producing out/<run-slug>/candidates.csv

# 3. Parse produced findings
python scripts/evalctl.py parse 2024-07-reserve \
  --workdir ~/openaudit/eval-runs/2024-07-reserve \
  --candidates ~/openaudit/out/<run-slug>/candidates.csv

# 4. Score (deterministic shortlist; optional LLM tie-break via --judge-cmd)
python scripts/evalctl.py score 2024-07-reserve \
  --workdir ~/openaudit/eval-runs/2024-07-reserve \
  --threshold 0.34 --judge-floor 0.18
```

Outputs, under `--workdir`:

| Path | Meaning |
|---|---|
| `_answer_key/ground_truth.jsonl` | unique H/M answer key (title, severity, body, issue URL) |
| `_answer_key/meta.json` | slug/title/date/contest + counts + dup stats |
| `target/` | blind source tree (git clone at HEAD) |
| `produced.jsonl` | normalized produced findings |
| `score.json` | machine-readable metrics |
| `scorecard.md` | human scorecard (missed GT, FPs, duplicates) |
| `BLIND_BRIEF.md` | scope + rules handed to the audit run |

## Metrics (how to read the scorecard)

- **Recall (all)** = unique GT H/M matched by ANY produced row / total GT H/M.
  "Did openaudit surface the bug at all?"
- **Recall (PoC-backed)** = same, restricted to produced rows that are BOTH
  confirmed (`status=confirmed-*` or `referee_verdict=CONFIRMED`) AND backed by a
  PoC (`poc_status` marks executable/differential/written/passing/verified/poc).
  This is the submission-grade metric.
- **Recall (read-confirmed)** = same, restricted to confirmed rows regardless of
  PoC. The gap vs PoC-backed = findings that look right but are unproven.
- **Precision (PoC-backed / read-confirmed)** = aligned submitted H/M / all such
  H/M. Low value = many false positives reaching that grade. Only PoC-backed rows
  should be treated as real submissions; `confirmed-read` is never submittable.
- **Duplicates** = extra aligned produced rows beyond the first per GT root.
  This is the metric to minimize; it maps to C4/Sherlock duplicate penalties.
- **Per-severity recall** = separate High vs Medium recall (missing Highs hurts most).

Matching is a deterministic TF-IDF shortlist (cosine over title+body vs
title+summary+trigger). Each GT is matched by its best produced row; produced rows
above `--threshold` align to their best GT. Rows in `[--judge-floor, --threshold)`
are listed as low-confidence; pass `--judge-cmd` to let an LLM confirm them.

### `--judge-cmd` protocol

The command is run once per borderline pair on stdin as JSON:

```json
{"produced": {...}, "truth": {...}, "similarity": 0.27}
```

and must print `{"match": true|false, "reason": "..."}` on stdout. Example:

```bash
--judge-cmd 'opencode run --quiet "Decide if these describe the same bug; print JSON {match,reason}"'
```

If the judge fails, the pair stays unmatched (deterministic result is the floor).

## Blindness (critical)

The answer key must not reach the auditing agent, or the score is meaningless.
The harness keeps it in `_answer_key/` inside the workdir, but a strong model can
snoop sibling paths. For a **clean** measurement, either:

1. copy only `target/` to a separate machine/user/session and run openaudit there, or
2. fetch the answer key into a workdir sibling the audit session is told not to read
   (the `blind` brief states this explicitly), or
3. run `fetch --skip-target`, audit a pre-existing clean clone, and score later.

`blind` refuses to print the answer key and only surfaces scope + target path.

## Notes / limitations

- C4 `report.md` is already deduped; `data/*.json` is used for duplicate *statistics*,
  not to inflate the key.
- Some contests ship an empty High set (`2024-07-reserve` = 0 High / 7 Medium); read
  per-severity recall, not just the total.
- The target clone is `code-423n4/<slug>@HEAD` (C4 freezes these post-contest). Record
  the cloned SHA in `meta.json` for reproducibility.
- This harness scores *findings discovery*, not report prose quality.
