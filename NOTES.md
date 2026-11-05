# NOTES

## API keys

None. The solution is deterministic, runs on the Python 3.11 standard library only (`requirements.txt` is
empty on purpose) and never touches the network. No paid APIs.

## How it works

`submission/solution.py` returns a `Knowledge` object (`submission/tks/resolve.py`). Nothing is overwritten
on ingest: every snapshot keeps its own view of the site, built from de-duplicated page versions
(sha256 of the markdown), so any date can be answered from the site as it stood then.

Each page version is turned into three things:

- **Rate facts**: every markdown table row with a percentage becomes a fact carrying its caption, section,
  row labels, metric (APY / APR / rate / dividend rate), effective date ("Rates are effective as of ..."),
  term in months, balance or loan-amount tier, model-year range and flags (jumbo, IRA, business,
  refinance, intro). Short `label: N% APY` prose lines on authoritative pages are read the same way.
- **Hours blocks**: `**Lobby**` / `**Drive-up**` day lists.
- **Passages** for a BM25 index (domain synonyms, light stemming, stable tie-breaks).

A question is parsed into an intent (rate, hours, address, count, URL, general), product words, qualifiers
and a date ("as of May 2026" in the text works like `as_of`). Rate questions are answered only from rate
facts; everything else from the best passage. The answer text holds just the resolved values and their
qualifiers. Effective dates, quotes and "why I ignored that other number" go into `Source.quote` and
`conflict_note`, so no incidental number can be mistaken for a stale one.

## What "current" means (the rules)

1. **Time.** Use the newest snapshot on or before `as_of` (none means the latest). A page missing from a
   later crawl was not re-fetched rather than deleted, so its newest earlier version is carried forward
   with its old `fetched_at`.
2. **Authority.** Rate sheets and product pages beat disclosures, which beat PDFs and promo landing pages,
   which beat calculators, news and blog posts. Tag/author listings are ignored. This is why the stale
   5.34% on the vehicle calculator and `/switch-and-earn` never beats 5.39% on the auto-loan page.
3. **Ties** go to the later effective date, then the later `fetched_at`.
4. **Qualifiers are identity.** Term, jumbo vs regular, IRA (Roth / Traditional), personal vs business,
   purchase vs refinance, intro vs standard, balance tier, loan-amount tier and model year make different
   facts. A named tier answers with that row only. Nested ceilings ("$100,000 or less" also fits under
   "$300,000 or less") take the tightest one. "Highest" / "lowest" picks one row. Otherwise every tier of
   the one matched product is listed.
5. **Narrative counts** (e.g. "22 branch office locations" boilerplate) take the most recently published
   statement. The 23-branch releases predate the Hy-Vee branch closing on March 31, 2026.
6. **Honesty.** If no rate table (or authoritative rate sentence) covers the product and term, or a named
   branch has no published address or hours, the answer is a fixed refusal with `confident=False` and no
   digits at all (the scorer zeroes refusals that contain numbers).

`conflicts()` compares the same fact identity across pages and across crawls. A model-year row is keyed
by its rank in the table, so "2023-26" last year and "2024-27" this year are the same tier. It reports two
kinds: **cross-page** (a stale copy disagrees with the authoritative page in the newest crawl) and
**superseded** (an authoritative value changed between crawls). Each resolves to the current authoritative
value and says why.

## Evals and harness

The organizers' scorer is never re-implemented. `evals/run.py` drives `harness/scoring.py` over:

| set | items | what it tests |
| --- | --- | --- |
| `eval/public_eval.jsonl` | 40 | the organizers' practice set |
| `evals/paraphrase.jsonl` | 80 | 3 rewordings per grouped public question (consistency) |
| `evals/temporal.jsonl` | 44 | `as_of` at, between and inside snapshot dates, plus dates written in the question |
| `evals/refusal.jsonl` | 30 | unanswerable questions and look-alike answerable ones |
| `evals/extended.jsonl` | 58 | topics outside the public set (IRA, HELOC, business, fees, branches) |
| `evals/conflicts_gold.json` | 23 | verified contradictions with the correct winner |

The gold was written independently of the solution. Every item cites the page and snapshot holding its
value, and `evals/validate_gold.py` checks each expected value against that cited page with the scorer's
own matcher (all 356 values pass). Items are split into dev and holdout. Tuning used dev only, and the
holdout was opened once for aggregate error analysis.

The harness adds:

- a per-item failure taxonomy (trap hit, missing value, false or missed refusal, wrong source, error, slow)
- a per-item regression gate against `evals/baseline.json`
- answer equality under two `PYTHONHASHSEED` values in separate processes
- metamorphic checks (upper-casing, punctuation, filler words, `as_of=<latest>` equal to no `as_of`)
- a socket guard that fails on any network use
- p95 latency and ingest budgets
- SHA-256 pins on the snapshot zips

Current results (official scorer, public set, with the conflicts gold): **final 0.806**.

- accuracy 0.84, with zero stale-trap hits
- consistency 1.0 and speed 1.0 (p95 0.02 s)
- conflicts F1 0.57 with resolution accuracy 0.70
- ingest 3.6 s

Across all local sets, QA is 0.88 on dev and 0.84 on holdout. The organizers' selftest scores 1.0. The
starter scored 0.09.

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl     # official scorer
python evals/run.py --snapshots snapshots --checks -v                                # full harness
python evals/validate_gold.py --snapshots snapshots                                  # gold integrity
python -m unittest discover -s tests -t .                                            # unit + security
```

## Security (OWASP Agentic Skills Top 10)

Each mitigation is commented at its site in the code.

- **AST05 Untrusted External Instructions**: page text is data only. Answers are extractive and authority
  ranked, so an injected "the rate is 0.01%" on a blog cannot win (a fixture test covers this).
- **AST04 Insecure Metadata**: manifest fields are validated, the crawl's own host is learned and foreign
  hosts are dropped, and nothing is deserialized beyond plain strings.
- **AST03 Over-Privileged / AST06 Weak Isolation**: no network, subprocess, file or dynamic-code use in
  the submission. An AST test and a runtime socket guard enforce this.
- **AST02 Supply Chain / AST07 Update Drift**: zero dependencies, CI actions pinned to commit SHAs, and
  snapshot zips pinned by SHA-256.
- **AST08 Poor Scanning**: structural tests scan for banned imports and calls, secrets in tracked files,
  redistributed snapshot text and hard-coded public answers.
- **AST09 No Governance**: every answer and conflict names its page, crawl and quoted row.
- **AST01 / AST10 Malicious Skills, Cross-Platform Reuse**: no agent, skill or tool-configuration files
  are part of the repository.

## Limits

Free-text questions rely on lexical retrieval. A reworded question that shares few words with the page
(e.g. "who is eligible" vs "you can find us in Iowa, Illinois and Wisconsin") can miss. That's the place
an embedding model would help, but here it would cost dependencies and determinism. Prose-only numbers
in news posts (Forbes "fifth time") are not compared for conflicts.

The snapshot data, the assignment PDF and the kit zips are not in this repository. Place the kit's
`snapshots_zip/` (or unpacked `snapshots/`) next to `harness/` to run the evals.
