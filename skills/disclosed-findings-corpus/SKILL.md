---
name: disclosed-findings-corpus
description: Ground hypothesis generation against a searchable corpus of past disclosed vulnerability findings (Web3 Audit Corpus: C4/Sherlock/Cantina/Spearbit/firm reports, DeFiHackLabs real exploit PoCs, Rekt post-mortems), for protocols where blind-only discovery risks missing known bug patterns. Trigger alongside the other Category B pipelines in Step 5, or when the target uses a common DeFi primitive (vault, AMM, lending, bridge) that past disclosed bugs cover well.
---

# disclosed-findings-corpus

A Category B pipeline, same tier as pashov/trailofbits/kadenzipfel/etc. - it
produces a report and answers kritt.md rows exactly like those do. What makes
this one different: instead of a fixed, author-written checklist, it searches
a growing corpus of ~46,000 real disclosed findings (Web3 Audit Corpus:
Code4rena, Sherlock, Cantina, Spearbit, Trail of Bits, OpenZeppelin, Cyfrin,
Pashov and more) plus real exploit PoCs (DeFiHackLabs) and post-mortems (Rekt).

## Why this runs ALONGSIDE the blind pipelines, not instead of them

openaudit's own discovery-stays-blind rule (Step 7) exists because priming an
agent with "here's what this bug class looks like" risks severity anchoring
and pattern-matching laziness. That's a real risk for a strong model with its
own latent knowledge of bug patterns. It's a worse tradeoff for a WEAK model -
less to draw on without grounding means lower recall, not better judgment.

The fix here isn't to override the blind rule, it's to make this one
pipeline's hint-matching safe to combine with it: **a retrieved pattern is
never itself a finding.** You may only report ANSWERED if you can cite the
exact line(s) in THIS target's own code that cause the problem - restating
the retrieved pattern's description, with no line of the target's own code to
point to, is REFUTED or UNANSWERED, never ANSWERED. Run this pipeline in
parallel with the blind ones; let Step 5.5's referee weigh all of them
together, same as any other pipeline's candidates.

## Setup (once per machine, not per run)

Retrieval uses SQLite FTS5 from the Python stdlib - **no pip install, no
sklearn/scipy/numpy, no embeddings API**. Ingestion of Solodit/DeFiHackLabs
needs only `requests` (already in the openaudit venv); the optional Rekt
scraper additionally needs `beautifulsoup4`.

```bash
cd skills/disclosed-findings-corpus
python scripts/ingest/ingest_findings.py data/sample_findings.jsonl   # tiny starter set
python scripts/retrieval/index.py                                     # smoke-test FTS5 retrieval
```

Build the main corpus from the open Web3 Audit Corpus snapshot (no API key):

```bash
mkdir -p ~/.local/share/openaudit/corpus/web3-audit
cd ~/.local/share/openaudit/corpus/web3-audit
curl -L -C - -O https://github.com/DicksonWu654/web3-audit-corpus/releases/latest/download/findings.jsonl.gz
curl -L -O https://github.com/DicksonWu654/web3-audit-corpus/releases/latest/download/DATA_DICTIONARY.md
curl -L -O https://github.com/DicksonWu654/web3-audit-corpus/releases/latest/download/LICENSE-DATA.md
cd -

python scripts/ingest/sources/web3_audit.py
python scripts/ingest/ingest_findings.py data/web3_audit.jsonl
```

That keeps EVM-ish High/Medium/Critical findings with usable body text, maps
`vuln_leaf` to `bug_class`, infers `protocol_type`, and keeps the original
report URL. Narrow it with `--firms code4rena sherlock cantina spearbit` or
probe with `--limit 1000`. The raw snapshot is ~97 MB compressed; the SQLite
DB with FTS is ~800 MB and lives at `data/findings.db` (gitignored). Re-run
the two scripts after a new release to refresh. Derived metadata is CC BY 4.0;
finding text stays with the publisher, so keep the URL with every citation.
The corpus is AI-extracted: verify the linked original before relying on a row.

Optional extra sources (each is independent):

```bash
export CYFRIN_API_KEY="..."   # solodit.cyfrin.io -> profile -> API Keys
python scripts/ingest/sources/solodit.py --impact HIGH MEDIUM --out data/solodit.jsonl
python scripts/ingest/sources/defihacklabs.py --since 2023-01 --out data/defihacklabs.jsonl
python scripts/ingest/sources/rekt.py --limit 50 --fetch-articles --out data/rekt.jsonl
```

**Classify a batch** (the Solodit/DeFiHackLabs/Rekt scripts leave
`bug_class`/`protocol_type`/`root_cause`/`pattern` empty on purpose - that's
your reasoning, not a second API call from inside the script; the Web3 Audit
adapter fills them from the corpus metadata and body text): read a batch of the output JSONL, and for
DeFiHackLabs records use `poc_source` (the real exploit code), for Rekt
records use `_article_text` - infer and fill in the schema fields yourself,
write the completed JSONL, then:
```bash
python scripts/ingest/ingest_findings.py data/your_classified_batch.jsonl
```
This is slow to do exhaustively (900+ DeFiHackLabs files, ~330 Rekt entries) -
classify a relevant slice (matching the target's protocol type) rather than
everything, and re-run as you classify more over time; `ingest_findings.py`
is append-only, so partial batches accumulate.

**Blind-eval note:** when replaying a contest, do not query this corpus for
that contest/firm during discovery. Exclude it (`--firms` on ingest, or a
separate DB) until `candidates.csv` is written, otherwise recall is
contaminated. The corpus is for Step 5's parallel Category B pass and Step 7
dedup, not for the blind discovery run.

**Refresh:** `web3_audit.py` reads the release file and writes a filtered
JSONL; `ingest_findings.py` appends by `finding_uid` and rebuilds the on-disk
FTS tables (`hint_fts`, `dedup_fts`). Queries are milliseconds after that.

## Running this pipeline on a target

For each in-scope file/function (same scope as every other Category B
pipeline, from SCOPE.md / in-scope-files.txt):

1. **Query for hints**, scoped by the target's protocol type:
   ```bash
   python scripts/retrieval/query.py "deposit mints shares, asset donated directly to vault" --protocol-type vault --top 3
   ```
   This returns the closest patterns, each with severity/bug_class/root_cause/
   preconditions. Write the query as a plain description of what THIS code
   does (its behaviour), not as a bug name - you are asking "what does code
   shaped like this break as elsewhere?", not "confirm my hypothesis".

2. **Decide if a hint applies**, with the hard citation rule from above. For
   each retrieved hint, ask: does this target's code have a LINE that causes
   the same failure, not just a resemblance in shape? If yes, this becomes a
   finding candidate with a cited line; if no, note it as considered-and-
   refuted (with the reason) in your dropped-candidates log, same as any
   other pipeline.

3. **Write findings** to `out/{protocol_slug}/reports/disclosed-findings-corpus.md`,
   following the same canonical schema every other Category B pipeline uses
   (id, source_pipeline, severity_claim, vulnerability_type, file, line,
   summary, malicious_actor, trigger_flow, malicious_input_example,
   exploitable) - `source_pipeline: disclosed-findings-corpus`, plus note
   which retrieved finding (title + original source) grounded each one, for
   the referee's dup-risk reasoning later.

4. **Answer kritt.md rows** exactly like every other Category B pipeline does
   in Step 5 - this pipeline's angle is an ADDITIONAL vote on Tier S rows, not
   a replacement for whichever pipeline already answered a row.

## Dedup search is a SEPARATE job from hint search - don't conflate them

`hint_search()` above is abstract pattern matching, used during discovery.
This corpus is ALSO useful for Step 7's existing-audit-report search, but
that's a different query:
```bash
python scripts/retrieval/query.py "oracle spot price used for collateral, no TWAP" --dedup --protocol-name MyProtocol --top 5
```
`dedup_search` is exact-match scoped to the SAME named protocol, used at
confirm/report time only, same blind-discovery-stays-blind rule as Step 7
already follows. Don't call it during Step 5 - that's exactly the anchoring
risk the blind rule protects against; it belongs in Step 7, alongside
everything else Step 7 already does.

## What this pipeline does NOT do

No LLM calls happen inside any script here - every classification and every
"does this hint apply" judgment is your own reasoning, using whatever model
is running this session. Retrieval is SQLite FTS5 bm25 (zero API calls, zero
third-party deps). There is no embeddings backend; if you later want semantic
search, add it as a separate opt-in module rather than making it the default.

## Honesty notes carried over from where these scripts came from

- `scripts/ingest/sources/solodit.py`: the request side (endpoint, header,
  rate limit) is built from Cyfrin's documented spec; the exact response
  field names are undocumented, so the mapping is defensive and logs the
  first result's raw keys - check that output on your first real run.
- `scripts/ingest/sources/rekt.py`: the leaderboard's content was confirmed
  real; its HTML selectors are inferred from rendered content, not verified
  byte-for-byte against raw HTML. Run `--limit 3` first and check the output.
- `scripts/ingest/sources/defihacklabs.py`: tested against the real repo,
  ~75-86% header-parse coverage depending on date range; `poc_source` (the
  actual exploit code) is kept for every file regardless of header-parse
  success.
- `scripts/ingest/sources/web3_audit.py`: the release is AI-assisted
  extraction/classification with measured error rates; some publisher bodies
  are withheld. Every row keeps `source_url`, and `as_dedup_record()` prints
  it. A corpus hit is a hint, never a finding.
