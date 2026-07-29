# ADR 0009 — 2025-paper adoption: sufficiency gate & BBQ-quantized vectors

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

ADR 0008 set the adoption pattern for published techniques: implement, sweep, publish the
numbers, keep what earns its place. This round applies it to two 2025-generation results
that target the stack's two remaining soft spots:

1. **Hallucination risk concentrates where retrieval is weak.** The post-hoc judge
   (ADR 0004) grades answers *after* the PRIMARY-tier model has already generated them —
   the most expensive way to find out the context never contained the answer.
   *Sufficient Context: A New Lens on Retrieval Augmented Generation Systems* (Joren et
   al., ICLR 2025) shows models hallucinate most when context is insufficient rather than
   contradictory, and that a lightweight sufficiency autorater in front of generation cuts
   exactly that failure mode.
2. **Float32 HNSW is the memory ceiling of vector search.** *RaBitQ* (Gao & Long, SIGMOD
   2024) proves 1-bit quantization with rigorous error bounds can preserve ranking quality
   when paired with oversampled rescoring against raw vectors; Elasticsearch shipped it as
   **BBQ** (`bbq_hnsw`), GA in the 8.18 line (2025). ~32× less vector memory in the HNSW
   graph, raw floats kept on disk for rescoring.

## Decision

**1. Two-stage sufficiency gate before generation.** `SufficiencyCheck` runs between
retrieval and the PRIMARY call:

- **Stage 1 is free:** if the cross-encoder's top score clears
  `RAG_SUFFICIENCY_CONFIDENCE_THRESHOLD` (default 0.35), generation proceeds with zero
  added latency — confident retrievals never pay for a second opinion. The reranker score
  we already compute doubles as the paper's "confidence signal".
- **Stage 2 is cheap:** below the threshold, a CHEAP-tier autorater answers one word —
  `SUFFICIENT | INSUFFICIENT` (same single-word contract the judge uses, lenient parse,
  `INSUFFICIENT` checked first since `SUFFICIENT` is its substring).
- **Insufficient → abstain early:** the client gets `sources`, a `sufficiency` SSE event,
  and the canned "I don't know" — no PRIMARY generation, no judge run, nothing cached.
  Abstention becomes the *cheapest* path instead of the most expensive one (ADR 0002's
  cost lens applied to a guardrail).
- **Fail-open:** error/timeout/no-verdict → generate anyway; the post-hoc judge still
  guards the output. Defense in depth: pre-gate (input quality) + post-judge (output
  quality), both fail-open, both metered
  (`recall_rag_sufficiency_{skips,verdicts}_total`).

**2. BBQ quantized vectors with oversampled rescoring.** ES 8.15 → 8.18.2 (server, client,
and the idempotency IT's Nori image recipe); the `embedding` field gains
`index_options: { type: bbq_hnsw }` and kNN queries add `rescore_vector.oversample: 3` —
walk the quantized graph wide, rescore the oversampled top set against raw floats. Guards:

- dims < 64 (tests, toy configs) fall back to plain float HNSW — BBQ's hard minimum,
  handled in `ensureIndex` rather than as a cryptic ES mapping error;
- the idempotency IT now runs at 64 dims so the BBQ mapping path is exercised on every CI
  run;
- **recall retention is not assumed — the eval gate (ADR 0007) re-runs on the quantized
  index and must stay above its thresholds.** That is the whole reason the gate exists:
  infra changes with quality risk get merged with the proof attached.

## Alternatives considered

- **Sufficiency check on every query** (no confidence threshold): simpler, but adds a
  CHEAP-tier call of TTFT to *every* question when the overwhelming majority retrieve
  confidently. The two-stage design keeps p50 untouched and spends latency only where the
  paper says the risk lives.
- **Rank1 / reasoning rerankers (2025)**: test-time-compute reranking is the current
  quality frontier, but minutes-per-query on local CPU generation. Deferred until an
  API-tier budget exists.
- **ReasonIR (2025)**: requires training a retriever; out of scope for a self-hosted
  portfolio stack.
- **Qwen3-Embedding / Qwen3-Reranker (June 2025)** as an A/B embedder swap: blocked by a
  real dependency conflict — the sidecar pins `transformers==4.45.2` (FlagEmbedding/bge
  runtime fix) while Qwen3 architectures need ≥ 4.51. Unblocking means re-validating the
  bge path on a newer transformers; queued as its own change, not smuggled into this one.
- **int8/int4 scalar quantization** instead of BBQ: the safe middle ground (4–8× memory),
  but RaBitQ's 1-bit + rescore is the published stronger result and ES ships it GA — and
  the gate can prove it holds here.

## Consequences

- **+** Insufficient-context questions now cost one CHEAP call instead of a PRIMARY
  generation *plus* a judge run — the guardrail saves money instead of spending it.
- **+** Vector memory for the HNSW-resident part drops ~32× (1024-dim float32 4 KB/vector
  → ~128 B quantized), which is the difference between "fits in RAM" and "doesn't" at
  corpus scale; verified recall-neutral at this corpus by the gate.
- **+** ES 8.18 also brings the `rescore_vector` API used here.
- **−** Low-confidence questions gain CHEAP-tier latency before abstaining or generating —
  bounded by `RAG_SUFFICIENCY_TIMEOUT_SECONDS`, and only below the confidence threshold.
- **−** ES major-line bump (8.15 → 8.18): Nori image, client and IT recipes all move
  together in this change; compose volumes carry the old index, so local stacks must
  delete + re-seed to pick up the quantized mapping (`ensureIndex` creates, it does not
  migrate).

## Validation

- `SufficiencyCheckTest`: confident retrieval skips the LLM (skip counter); INSUFFICIENT
  blocks; SUFFICIENT allows; substring-trap parse; error fails open; disabled never calls.
- Full backend suite on the ES 8.18.2 + Nori Testcontainers image — BBQ mapping exercised
  at 64 dims.
- Live: index recreated with `bbq_hnsw`, corpus re-seeded through the pipeline, **eval
  gate re-run on quantized vectors — PASS required**; out-of-corpus question demo:
  `sufficiency` event → early abstention with the amber badge.
