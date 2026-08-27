# Eval harness

Measures retrieval quality so improvements are **numbers, not vibes** (docs/adr/0001),
enforces them in CI so regressions are **build failures, not surprises** (docs/adr/0007),
and reports them with the inference that makes a number a claim rather than an anecdote
(docs/adr/0011).

```bash
python run_eval.py gold.jsonl                                  # comparison table + intervals
python run_eval.py gold.jsonl --gate                           # CI mode: exit 1 on regression
python run_eval.py gold.jsonl --json out.json --markdown summary.md
python run_eval.py gold.jsonl --gate --gate-policy sequential   # stop when decided
python power_report.py gold.jsonl                              # what this gold set can resolve
python anytime_experiment.py                                   # reproduce the ADR-0012 tables
python -m unittest discover -s . -p "test_*.py"                # test the harness itself
# RECALL_API=http://localhost:18080 python run_eval.py gold.jsonl
```

- Gold set format: JSONL, one `{ "query": "...", "relevant_doc_ids": ["..."] }` per line.
- **Sweeps `bm25` / `vector` / `hybrid` automatically** (via `/api/search?mode=`) and reports
  **Recall@5, Recall@10, MRR@10, nDCG@10** per mode in a single run, plus the first-relevant
  rank per query — the hybrid lift is the README headline number.
- Metrics are **doc-level**: rankings come back chunk-level, so ranked docIds are
  deduplicated by first occurrence before scoring (otherwise one document's chunks occupy
  several ranks and DCG can exceed the ideal DCG).
- `--modes` extends the sweep with the experimental paper modes (docs/adr/0008):
  `hybrid-m3` (bge-m3 tri-modal self-hybrid rerank, deterministic) and `hyde`
  (LLM hypothetical-document embeddings — needs a provider; free local Ollama works).
  `make eval-sweep` runs all five. CI keeps the deterministic default sweep.
- CI: `.github/workflows/eval.yml` boots the real stack, seeds the corpus through the async
  pipeline (`scripts/seed_corpus.py` polls ES until all docs are searchable), then runs the
  gate on every retrieval-affecting PR.

## Every number comes with its uncertainty

A retrieval score is an estimate from a finite query sample. `stats.py` (standard library
only, seeded, [ADR 0011](../docs/adr/0011-statistical-inference-eval.md)) supplies:

- **Intervals on every metric.** Exact binomial (Clopper-Pearson) where the per-query
  metric is 0/1 — which is what Recall@k is when each query has one relevant document —
  and BCa bootstrap otherwise. `Recall@5 = 1.00` over ten queries is `[0.69, 1.00]`; the
  bootstrap would have said `[1.00, 1.00]`, which is why the choice of interval is made
  from the metric's support rather than by habit.
- **Significance against the baseline mode.** Paired randomization test on per-query
  scores (Smucker, Allan & Carterette, CIKM 2007) — no normality assumption, which matters
  because reciprocal rank is discrete and skewed. p-values are **Holm-corrected** across
  the modes compared within each metric: a five-mode sweep is five chances at a false win.
- **`effective_n` and the p-value floor.** Queries that score identically under both
  systems carry no signal, so the sample size that counts is the number that *differ*. A
  paired test on `k` differing queries cannot return a two-sided p below `2 / 2^k`. When
  that floor is already above 0.05 the comparison is reported as `unresolvable` — a fact
  about the gold set, not a result about the system.

## `power_report.py` — what the gold set can resolve, before you run anything

```bash
python power_report.py gold.jsonl
python power_report.py gold.jsonl --from-json rag-eval-results.json   # measured spread
python power_report.py gold.jsonl --markdown design.md
```

Answers three questions from the labels alone — no stack, no network:

1. **The p-value floor** at each number of differing queries, and the first `k` that can
   clear 0.05 at all.
2. **The interval width** you are stuck with at this `n`, including the one a perfect score
   buys you.
3. **The queries you would need** to resolve a target improvement at 80% power, bracketed
   across plausible per-query spreads (or the measured one, with `--from-json`).

`make eval-stats` runs it against the shipped gold set.

## Anytime-valid evaluation — stop when the verdict is decided

Evaluation is sequential: queries are scored one at a time. It is *analysed* as a single
batch, and that mismatch costs twice.

**It invalidates the statistics.** A 95% interval is a promise about one look at a
finished sample. Watching a CI log and forming a view before it ends is peeking, and the
guarantee does not survive it. Measured, with the fixed-N interval given every benefit of
the doubt (inspection only starts once the normal approximation is defensible, at n = 30):

| metric | queries | fixed-N interval | confidence sequence |
|---|:---:|:---:|:---:|
| Recall@5 (0/1) | 300 | **34.0%** | 1.9% |
| reciprocal rank (discrete, skewed) | 300 | **32.8%** | 2.6% |
| nDCG@10 (graded) | 300 | **35.1%** | 1.6% |
| groundedness judge (skewed high) | 300 | **30.0%** | 0.5% |

Share of runs where the true mean falls outside a nominal 95% interval at *some* point
while it is being watched. The fixed-N column rises with stream length — the longer you
watch, the more chances to escape — which is the signature of the law of the iterated
logarithm and a sign the simulation is behaving. Full table across three stream lengths:
`make eval-anytime`.

**It wastes the budget.** A gate asking "is MRR@10 above 0.85?" against a system scoring
0.95 is settled long before the last query. `--gate-policy sequential` scores queries one
at a time behind an anytime-valid confidence sequence
([`sequential.py`](sequential.py), Waudby-Smith & Ramdas, JRSS-B 2024) and exits the loop
the moment the verdict is determined — the unspent budget is HTTP requests and LLM-judge
calls never made:

| true Recall@5 | vs a 0.85 line | mean stop (of 300) | queries saved | wrong |
|:---:|:---:|:---:|:---:|:---:|
| 0.98 | pass | 54 | **82%** | 0.0% |
| 0.95 | pass | 89 | **70%** | 0.0% |
| 0.85 | at the line → `undecided` | 293 | 2% | 1.8% |
| 0.60 | fail | 31 | **90%** | 0.0% |

A system sitting on the line is reported `undecided` rather than nudged either way, and
`undecided` fails the gate — a gate that treats "we could not tell" as a pass has quietly
stopped gating. Type-I error at the line stays inside α.

Retrieval metrics suit this construction unusually well: reciprocal rank, nDCG, recall and
a judge's score all live in [0, 1], which is exactly the regime betting confidence
sequences are built for, and no distributional assumption is needed — reciprocal rank is
discrete and skewed enough that a normal approximation is a poor fit at any n.

One caveat, stated rather than buried: the target is the **superpopulation** mean — the
score the system would get on the query distribution the gold set is drawn from, not the
exact average of these particular queries. That is the quantity worth gating on, and it
requires a random evaluation order, so the gate shuffles with a recorded seed rather than
reading the file top to bottom. Design: [ADR 0012](../docs/adr/0012-anytime-valid-evaluation.md).

## Gate policies

`--gate` enforces one of four policies on `--gate-mode` and exits non-zero on breach.
Thresholds default to Recall@5 ≥ 0.90, MRR@10 ≥ 0.85, nDCG@10 ≥ 0.85 — deliberately below
measured values; a gate catches regressions, it isn't a leaderboard.

| `--gate-policy` | fails when | use it when |
|---|---|---|
| `point` *(default)* | the mean falls below the threshold | always — the absolute floor |
| `ci-lower` | the 95% lower bound falls below the threshold | the gold set is large enough that the interval is narrower than the gate's safety margin |
| `regression` | *also* when a paired randomization test against `--baseline-json` shows a significant drop | you have a recorded green run to compare against |
| `sequential` | the anytime-valid verdict is `fail`, or the budget runs out `undecided` | the gold set is large and each query costs money or minutes |

`regression` is the sensitive one. An absolute threshold only notices a regression once the
mean crosses a line someone guessed; the paired test compares the *same queries* before and
after, so a real drop on two queries is caught while the mean still clears the line. It
needs a previous `--json` result — commit one, or restore the last green run's artifact.

`ci-lower` is deliberately not the default: on a ten-query gold set the interval is wider
than the whole safety margin (`Recall@5` bottoms out at 0.69 against a 0.90 line), so
adopting it is a decision to grow the gold set first, not a decision to loosen the gate.

`sequential` evaluates only the gate mode — it answers one question as cheaply as
possible, and sweeping the other modes for a comparison table would spend exactly the
queries it exists to save. It also short-circuits: the first metric to fail ends the run,
because the build is already red and confirming the rest buys nothing.

`--markdown` renders the tables, significance verdicts and design analysis for the GitHub
step summary; `--json` dumps everything, per-query detail included.

## The harness is tested

`test_stats.py`, `test_sequential.py`, `test_run_eval.py`, `test_tune.py` and
`test_power_report.py` run offline against a stubbed search backend — 122 tests, standard
library only, no stack required:

- the statistical layer against **closed forms** (`(α/2)^(1/n)` at `k = n`, `2^(1-k)` for a
  uniform improvement, R's `p.adjust` worked example), an **independent brute-force**
  permutation reference, **empirical BCa coverage** against a skewed population, and
  **SciPy cross-checks** that skip when SciPy is absent;
- the anytime-valid layer against the property that actually matters — coverage under
  continuous inspection, simulated side by side with the fixed-N interval it replaces —
  plus type-I error at the threshold, which is where a sequential test is easiest to get
  wrong;
- the harness against rankings whose metrics are derivable by hand, driving all four gate
  policies into both outcomes and asserting that the sequential one's saving is real
  requests not sent.

An eval harness that gates CI is production code: if it computes nDCG wrong, nothing else
in the build will say so. `make eval-test`, and CI runs it on every push.

## RAG QA eval (groundedness)

`run_qa_eval.py` drives the full RAG path (`/api/ask`, SSE) over the same gold set and
aggregates the answer-quality signals produced by the post-hoc LLM judge (docs/adr/0004):

```bash
python run_qa_eval.py gold.jsonl
# RECALL_API=http://localhost:18080 python run_qa_eval.py gold.jsonl
```

Reports per query and in summary:

- **groundedness** — judge verdict split (supported / partial / unsupported) + average score
- **citation coverage** — share of generated answers containing at least one `[n]` citation
- **abstention** — answers that (correctly) said "I don't know"; never graded as hallucinations
- **TTFT p50 / e2e p50** — streaming latency seen by the client

Cache hits are reported separately (cached answers skip the judge). Requires the stack up
and an LLM provider configured — the free local `ollama` provider works.
