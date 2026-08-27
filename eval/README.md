# Eval harness

Measures retrieval quality so improvements are **numbers, not vibes** (docs/adr/0001),
enforces them in CI so regressions are **build failures, not surprises** (docs/adr/0007),
and reports them with the inference that makes a number a claim rather than an anecdote
(docs/adr/0011).

```bash
python run_eval.py gold.jsonl                                  # comparison table + intervals
python run_eval.py gold.jsonl --gate                           # CI mode: exit 1 on regression
python run_eval.py gold.jsonl --json out.json --markdown summary.md
python power_report.py gold.jsonl                              # what this gold set can resolve
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

## Gate policies

`--gate` enforces one of three policies on `--gate-mode` and exits non-zero on breach.
Thresholds default to Recall@5 ≥ 0.90, MRR@10 ≥ 0.85, nDCG@10 ≥ 0.85 — deliberately below
measured values; a gate catches regressions, it isn't a leaderboard.

| `--gate-policy` | fails when | use it when |
|---|---|---|
| `point` *(default)* | the mean falls below the threshold | always — the absolute floor |
| `ci-lower` | the 95% lower bound falls below the threshold | the gold set is large enough that the interval is narrower than the gate's safety margin |
| `regression` | *also* when a paired randomization test against `--baseline-json` shows a significant drop | you have a recorded green run to compare against |

`regression` is the sensitive one. An absolute threshold only notices a regression once the
mean crosses a line someone guessed; the paired test compares the *same queries* before and
after, so a real drop on two queries is caught while the mean still clears the line. It
needs a previous `--json` result — commit one, or restore the last green run's artifact.

`ci-lower` is deliberately not the default: on a ten-query gold set the interval is wider
than the whole safety margin (`Recall@5` bottoms out at 0.69 against a 0.90 line), so
adopting it is a decision to grow the gold set first, not a decision to loosen the gate.

`--markdown` renders the tables, significance verdicts and design analysis for the GitHub
step summary; `--json` dumps everything, per-query detail included.

## The harness is tested

`test_stats.py`, `test_run_eval.py` and `test_power_report.py` run offline against a
stubbed search backend — 70 tests, standard library only, no stack required:

- the statistical layer against **closed forms** (`(α/2)^(1/n)` at `k = n`, `2^(1-k)` for a
  uniform improvement, R's `p.adjust` worked example), an **independent brute-force**
  permutation reference, **empirical BCa coverage** against a skewed population, and
  **SciPy cross-checks** that skip when SciPy is absent;
- the harness against rankings whose metrics are derivable by hand, driving all three gate
  policies into both outcomes.

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
