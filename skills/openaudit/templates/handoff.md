# Handoff - `out/{slug}/handoff/` (operator-owned; read by every remaining Category B agent)

Three artifacts. The operator updates `submitted-findings.md` LIVE as submissions go out. Agents
read all three at start and dedup-check every candidate against them BEFORE promotion.

## submitted-findings.md
Findings the operator obtained from a subagent and DECIDED TO SUBMIT.

| id | title | sev | file:line | root cause | dup_signature | submission ref | date |
|----|-------|-----|-----------|-----------|---------------|----------------|------|
| SUB-01 | <title> | Medium | src/Foo.sol:42 | <one-line root cause> | ROOT:<canonical-key> | <URL/thread> | YYYY-MM-DD |

## known-issues.md
Program-published known issues / acceptable risks. Quote the source (SCOPE.md line, README,
prior-report section). A candidate matching one becomes `dup-of:KNOWN` and is not promoted.

| id | summary | source (verbatim) | scope of acceptance |
|----|---------|-------------------|---------------------|
| KNOWN-01 | <summary> | SCOPE.md:NN "...": | <what it covers> |

## prior-audits/
Index of prior audit reports gathered in Step 7. One line per report:

- `<source/author>` - `<path>` - covers `<contracts/files>` - findings used as known-issue input.
