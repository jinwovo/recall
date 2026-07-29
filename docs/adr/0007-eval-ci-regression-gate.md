# ADR 0007 — Retrieval eval as a CI regression gate

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

The project's founding claim is "measured, not asserted" (ADR 0001): the hybrid pipeline's
lift over BM25 is a table of numbers produced by `eval/run_eval.py` against a labeled gold
set. But those numbers were measured **once**, by hand, on a developer machine. Nothing
stopped a future change — a chunker tweak, an analyzer swap, an RRF constant, a reranker
upgrade — from silently regressing retrieval quality while every unit test stayed green.
Retrieval quality is exactly the kind of property that decays invisibly: no exception is
thrown when Recall@5 drops.

Two harness defects also surfaced while wiring this up, both fixed here because a gate is
only as trustworthy as its metric:

- rankings are **chunk-level** but the gold set is **doc-level**; without deduplication a
  document with several matching chunks occupied several ranks, wasting recall cutoffs and
  letting DCG exceed the ideal DCG (nDCG > 1.0 was reachable);
- a single `EVAL_K` conflated ranking depth with reporting cutoffs, so the published
  Recall@5 and MRR@10 came from separate runs.

## Decision

**1. Doc-level metrics, fixed cutoffs.** `run_eval.py` deduplicates ranked docIds by first
occurrence, then reports Recall@5, Recall@10, MRR@10 and nDCG@10 per mode in one run —
the same four columns as the README table, plus a per-query first-relevant-rank matrix
for diagnosis.

**2. A gate, not a leaderboard.** `--gate` enforces minimum thresholds on the hybrid mode
and exits non-zero on breach. Thresholds sit **below** the measured values by a safety
margin (measured ≈ 1.00 / 0.95 / 0.96 → gate ≥ 0.90 / 0.85 / 0.85): the gate exists to
catch *regressions*, not to flake on benign rank jitter between environments. Tighten as
history accumulates; never tune a threshold down to make a PR pass — that is the one move
the gate forbids.

**3. The real stack, in CI.** `.github/workflows/eval.yml` boots the production compose
topology (ES + Nori, bge-m3 + reranker sidecar, Kafka, MinIO, the backend) — not mocks —
because the claim under test spans the whole retrieval path: Korean analysis, cross-lingual
embeddings, RRF, cross-encoder scores. Determinism comes from pinned corpus + gold set,
pinned model names, and CPU inference; the ~4.6 GB of model weights are held in
`actions/cache` and bind-mounted into the sidecar (`docker-compose.ci.yml`).

**4. Async seeding needs a barrier.** Ingestion is async (ADR 0003), so "seeded" ≠
"searchable". `scripts/seed_corpus.py` POSTs the corpus through the real Kafka pipeline,
then polls an ES `docId` terms aggregation until every expected document is indexed —
a deterministic barrier instead of sleep-and-hope, and a free end-to-end smoke test of
the ingestion path on every gate run.

**5. Reviewable output.** The gate writes the mode-comparison table, threshold verdicts and
the per-query rank matrix to the GitHub step summary and uploads the full JSON as an
artifact — a regression shows *which query* fell to *which rank*, not just a red X.

**6. Scoped triggers.** The job runs on PRs and main pushes touching retrieval-affecting
paths (backend, sidecar, ES image, eval, compose, the workflow itself) plus manual
dispatch. README-only changes do not pay the ~15-minute stack boot.

## Alternatives considered

- **Mock the embedder in CI** (hash-based vectors): fast and hermetic, but it deletes the
  thing being measured — cross-lingual semantic retrieval. A gate on mock vectors gates
  nothing. Rejected.
- **Golden-file assertion on exact rankings**: brittle (any benign reorder fails) and
  uninformative (no notion of "how much worse"). Metric thresholds with margin fail only
  when quality actually drops.
- **Run the QA/groundedness eval in the same gate**: requires an LLM in CI; the free local
  path (Ollama on CPU runners) is minutes-per-query slow and judge verdicts add
  nondeterminism to a pass/fail signal. Retrieval gates on every PR; QA eval stays a
  measured-and-published number (README) until a budgeted API key makes it gateable.
- **Nightly instead of per-PR**: catches regressions after merge, when bisecting costs a
  day. Path filters keep the per-PR cost proportionate instead.

## Consequences

- **+** "Eval-driven development" is now enforced, not aspirational — challenge #6 of the
  project plan closes.
- **+** The gate exercises ingestion (Kafka barrier), indexing (Nori/kNN), fusion and
  rerank on every retrieval-touching PR — an integration test disguised as a benchmark.
- **+** Metric bugs fixed (doc-level dedupe); published numbers are re-measured under the
  corrected metric and reproducible by `make eval-gate`.
- **−** ~15 min wall-clock per gated PR (image builds dominate; model weights are cached).
  Acceptable for a quality gate; buildx layer caching is the known optimization if it
  starts to hurt.
- **−** 10 queries is a small gold set — thresholds carry wide margins accordingly.
  Growing the gold set tightens the gate for free.

## Validation

- Local: full stack up → `seed_corpus.py` barrier (24/24 docs) → `run_eval.py --gate`
  passes with margin; numbers in the README "Measured results" table re-measured under
  the doc-level metric.
- CI: the `eval / retrieval-gate` check runs this PR's own workflow — the gate gates the
  change that introduces it.
