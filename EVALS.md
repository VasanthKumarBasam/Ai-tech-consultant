# Evaluations

Every number below comes from a CSV written by an eval runner (`*/evaluation/results/`). `./start.sh eval` runs all three suites and copies the latest summaries into the marked sections with `scripts/fill_evals.py`. Runs without an API key use a stand-in model, are labelled "fake (not Claude)", and are not results. Failed cases are kept, never filtered out.

## 1. What is measured
| Dimension | Knowledge Brain | Inbox Agent | Meeting to Scope |
| --- | --- | --- | --- |
| Quality | pass rate; right article cited; key facts present; abstains when it should; screenshot evidence found; optional judge faithfulness (1-5) | category and risk accuracy; right article retrieved; key facts in the draft; optional judge scores (correctness, policy, tone, completeness) | requirement recall incl. status/priority; platform-check agreement; constraints/exclusions captured; change detection; estimate inside a reference band; testable acceptance criteria |
| Safety | prompt-injection cases; no answer without citation | unsafe drafts caught (refund/price/date promises, unknown links, leaks); recall of "needs a human" | approval gates; no false removals; out-of-scope additions flagged |
| Cost | tokens × price per model → $/query | $/email (classification + draft + KB lookups) | $/meeting, per stage |
| Speed | median and p90 latency | median and p90 per email | wall time per meeting |
| Reliability | errors, validation retries, ingestion page success rate | errors, retries | pipeline failures, retries, verified-quote rate |

Cost uses the per-million-token prices in code (`DEFAULT_PRICING`, overridable with `MODEL_PRICING_JSON`): Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 from $0.10/$0.50 input/output (models overview page, checked 2026-10-17; Haiku's listed price is a "from" price, so its cost here is a lower bound). Cache reads and writes are counted as input tokens at the normal rate, which slightly overstates cost.

## 2. Datasets
- **Knowledge Brain: 30 cases** (`task1-knowledge-brain/evaluation/kb_cases.jsonl`): 17 direct questions, 2 needing two articles, 2 paraphrased in customer language, 3 about screenshots, 4 unanswerable, 2 prompt-injection. Expected articles are real BD article ids; key facts were taken from the live articles. Questions resolved by experts are appended automatically (`escalated_cases.jsonl`).
- **Inbox: 50 role-played emails** (`task2-inbox-agent/data/emails.json`): 13 how-to, 9 troubleshooting, 6 project, 6 refund/cancellation, 6 complaints, 5 quotes, 5 follow-ups (threaded to earlier emails). Each has expected category, risk, whether a human is required, expected source articles, key facts, forbidden behaviours and the expected behaviour in words. **20 are held out** (`split: test`) and were not used while writing prompts; the eval defaults to them.
- **Meeting to Scope: 3 transcripts + 53 scored cases** (`task3-meeting-to-scope/evaluation/gold/`): A (discovery, 13k characters, so it's processed in 3 chunks), B (checkout/payments), C (follow-up that removes, adds and modifies requirements and moves the date). Per model the runner does A → approve → C → B and scores 20 requirement cases, 5 platform-check cases, 12 constraint/exclusion checks, 8 change-detection cases, 2 estimate bands, 2 evidence checks, 2 acceptance-criteria checks and 2 approval-gate cases. Reference estimate bands were set by the author from the transcripts (A: 55-130 h likely, B: 50-140 h) and should be reviewed by a delivery lead.

## 3. Model comparisons run
```bash
cd task1-knowledge-brain && ../.venv/bin/python evaluation/run_eval.py --models claude-haiku-5-5,claude-sonnet-5-5 --judge claude-opus-5-5
cd task2-inbox-agent && npm run eval -- --classify-models rules,claude-haiku-5-5,claude-sonnet-5-5 --draft-models claude-haiku-5-5,claude-sonnet-5-5 --judge claude-opus-5-5
cd task3-meeting-to-scope && ../.venv/bin/python evaluation/run_eval.py --models claude-sonnet-5-5,claude-opus-5-5,claude-haiku-5-5
```
The same frozen cases, prompts and settings (temperature 0 where allowed) are used for every model; only the model id changes. In the inbox, every draft model receives the same classifier output so differences come from drafting alone.

## 4. Results

### Knowledge Brain
<!-- RESULTS:task1:start -->
_Not run yet: `./start.sh eval`._
<!-- RESULTS:task1:end -->

### Inbox Agent
Measured during the build, no model needed: the keyword baseline classifier (`rules`) scores **90.0%** category accuracy on the 20 held-out emails but **76.0%** on all 50, and flags only **8 of 12** high-risk emails as high risk (it misses complaints and a refund request phrased without keywords). The rules were written by the author, so treat this as an optimistic floor that any model has to beat. Reproduce: `cd task2-inbox-agent && npm run eval -- --split all --classify-models rules --draft-models fake` (classification table only; the fake drafts are not a measurement).
<!-- RESULTS:task2:start -->
_Not run yet: `./start.sh eval`._
<!-- RESULTS:task2:end -->

### Meeting to Scope
<!-- RESULTS:task3:start -->
_Not run yet: `./start.sh eval`._
<!-- RESULTS:task3:end -->

## 5. Model decisions
| Decision | Evidence that decides it | Outcome |
| --- | --- | --- |
| KB answer model: Sonnet vs Haiku | answerable-correct and abstention within 5 points → cheaper model wins | _fill in after the run_ |
| Retrieval: hybrid vs vector vs keyword | Hit@5 and multi-article recall | _fill in_ |
| Email classifier: Haiku vs Sonnet vs rules | category accuracy, high-risk caught (a missed high-risk email is the costly error) | _fill in_ |
| Email drafter: Sonnet vs Haiku | unsafe drafts (must be 0 after guards), key facts, judge policy score | _fill in_ |
| Scope team: Sonnet vs Opus vs Haiku | change detection and requirement recall first, cost second | _fill in_ |

## 6. Reliability and safety tests (run on every change, no key needed)
`./start.sh test` runs 72 tests (35 Python for Task 1, 25 TypeScript for Task 2, 12 Python for Task 3). They exercise the failure paths with a fake help centre, a stub Knowledge Brain and a stand-in model: retries on 503 and dropped connections; a crawl interrupted mid-run resuming; a second run in another process refused; unchanged pages producing no new versions; changed and deleted articles disappearing from answers; the mass-deletion guard; rollback; robots.txt; invalid citations rejected; expert answers becoming retrievable (customer-specific ones not); Knowledge Brain down → holding reply that can't count toward autonomy; double sends prevented; the 20-in-a-row lifecycle including audit-flag shutdown; legal-threat emails never auto-sent in an enabled category; follow-up analysis refused without an approved baseline; edits after approval forcing re-approval; a model claiming an existing feature without evidence failing the stage.

## 7. Defects found during the build, and what changed
| Found by | Problem | Fix |
| --- | --- | --- |
| Task 3 test | a failed pipeline stage stayed "running" in the progress view (stale job record read before the run) | re-read the job on failure; test asserts the failed stage is marked |
| Task 2 test | the refund-promise guard flagged a correct reply ("I've passed your refund request to our billing team") | pattern narrowed to the promise verb itself; regression test added |
| Task 1 eval dry run | unit tests appended to the real expert-cases eval file | path comes from settings; tests use a temp file |
| Task 1 review | title extraction could take the site header's `<h1>` instead of the article title | article selectors, then `<title>` minus the site suffix, then `<h1>` |
| Task 1 review | a terminal crawl and the API's daily job could run at once and mark each other interrupted | run heartbeat; only runs silent for 10 minutes are marked interrupted; concurrent run refused (test) |
| Eval dry runs | stand-in runs were reported under Claude model names | runners label them "fake (not Claude)" |

## 8. Failure analysis (fill in from your run)
For the three most important failed cases per task, record: case id → what happened (from the CSV) → root cause (retrieval, prompt, data, or model) → change made → before/after on the same cases. Keep the failures in the table even after fixing them.
