# ADR 0001 — Hybrid retrieval: BM25 + dense vector fused with RRF, then reranked

- **Status:** Accepted
- **Date:** 2026-06-23

## Context

A knowledge-base search must return relevant chunks for both:
- **lexical / exact** queries (error codes, API names, config keys) — where keyword
  matching wins, and
- **semantic / paraphrased** queries ("how do I make X resilient?") — where dense
  embeddings win.

BM25 alone misses paraphrases; pure vector search misses exact tokens and rare
identifiers, and can drift on short queries. Korean adds morphology: naive
whitespace tokenization breaks recall.

## Decision

Use **hybrid retrieval**:

1. **BM25** over a `content` field analyzed with the **Nori** Korean analyzer
   (plus an English-analyzed field for EN docs).
2. **Dense kNN** over a `dense_vector` field (1024-dim `bge-m3`, multilingual).
3. **Fuse** the two ranked lists with **Reciprocal Rank Fusion (RRF)**:
   `score(d) = Σ 1 / (k + rank_i(d))`, `k = 60` to start.
4. **Rerank** the fused top-50 with a cross-encoder (`bge-reranker-v2-m3`) → top-K
   (default 8) for the RAG context.

RRF is chosen over learned/weighted fusion first because it needs no score
normalization and no training data, and is a strong baseline. The reranker is a
second stage precisely because cross-encoders are accurate but too expensive to
run over the whole corpus.

## Consequences

- **+** Higher recall than either retriever alone; measurable lift (Recall@10, MRR@10, nDCG@10).
- **+** Single store (Elasticsearch) for BM25 + vectors → simpler ops.
- **+** Multilingual (KO/EN) handled by `bge-m3` + Nori.
- **−** Two retrieval calls + a rerank hop add latency → mitigated by limiting
  fused candidates (50) and reranking only those; reranker runs in the sidecar.
- **−** RRF `k` and rerank top-K are tunables → owned by the eval harness, not guesswork.

## Alternatives considered

- **Vector-only** — simplest, but loses exact-match recall and rare identifiers.
- **BM25-only** — strong for lexical, weak on paraphrase; no cross-lingual.
- **Weighted linear fusion** — needs score normalization + tuning per query
  distribution; revisit if RRF plateaus.

## Validation

Eval harness (`eval/`) reports Recall@10 / MRR@10 / nDCG@10 for **vector-only**,
**BM25-only**, **RRF**, and **RRF+rerank** on the same query set. The hybrid lift
is a headline number in the README.
