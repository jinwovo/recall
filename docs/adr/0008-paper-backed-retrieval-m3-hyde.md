# ADR 0008 — Paper-backed retrieval upgrades: M3 tri-modal self-hybrid & HyDE

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

The retrieval pipeline (ADR 0001) is built from literature — BM25, RRF fusion (Cormack et
al., SIGIR 2009), dense multilingual embeddings and a cross-encoder reranker (both from the
BGE family, Chen et al. 2024) — but it uses each component in its most conventional shape.
Two published techniques apply directly to this exact stack and are measurable with the
eval harness that now gates CI (ADR 0007):

1. **bge-m3 is a tri-modal model and we were using one third of it.** The M3 paper
   (*BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text
   Embeddings Through Self-Knowledge Distillation*, Chen et al. 2024) trains dense,
   sparse-lexical and ColBERT-style multi-vector heads jointly and reports that a weighted
   **self-hybrid** of the three outperforms any single mode. Our sidecar already runs this
   model for dense embeddings — the other two heads were idle capability.
2. **HyDE** (*Precise Zero-Shot Dense Retrieval without Relevance Labels*, Gao et al.,
   ACL 2023): embed an LLM-generated *hypothetical answer document* instead of the query.
   Questions and answers live in different regions of embedding space; a hypothetical
   answer is shaped like the passages being sought. The paper's recipe needs only an
   instruction-following LLM — the free local Ollama provider qualifies.

## Decision

**1. M3 tri-modal scoring as a pluggable rerank strategy.** The sidecar gains
`POST /score_m3`: for (query, passage) pairs it returns dense, sparse and ColBERT
sub-scores plus their weighted sum (weights `[0.4, 0.2, 0.4]` — the dense/sparse/multivec
proportions the M3 paper uses for its hybrid evals). The hybrid path takes
`/api/search?rerank=` with `cross-encoder` (default, unchanged) or `m3`. Same candidate
generation, same RRF fuse — only the final ordering differs, so the eval harness compares
strategies head-to-head. Trade-off being measured: the cross-encoder scores query and
passage *jointly* (strongest signal, second model in memory); M3 self-hybrid reuses the
embedder (no extra model, one forward pass per pair, sparse head adds exact-term signal
the dense head can miss).

**2. HyDE as an experimental search mode.** `mode=hyde`: the CHEAP-tier model writes a
3-4 sentence hypothetical documentation passage for the question; that passage — not the
question — is embedded and sent to kNN. Fail-open: any LLM failure (no provider, timeout,
blank output) falls back to plain vector search over the original query and increments
`recall_search_hyde_fallbacks_total`; retrieval never breaks for lack of an LLM. Latency
is the honest cost (an LLM generation before retrieval), which is why HyDE is a mode, not
the default.

**3. Measured, not adopted-by-default.** Both techniques enter the eval sweep
(`--modes bm25,vector,hybrid,hybrid-m3,hyde`, `make eval-sweep`) and their numbers go in
the README next to the incumbents. The CI gate (ADR 0007) still runs the deterministic
default sweep — HyDE's LLM dependency would add nondeterminism to a pass/fail signal, and
`hybrid-m3` joins the gate only if it becomes the default strategy.

## Alternatives considered

- **GraphRAG (Microsoft 2024) / RAPTOR (Sarthi et al., 2024)**: LLM-built graph/tree
  indexes shine on corpus-global questions ("summarize themes across all docs") — a query
  shape this product doesn't serve, at an LLM-indexing cost that grows with the corpus.
  Rejected for scope, noted for a corpus-QA future.
- **Late chunking (Günther et al., Jina 2024)**: contextual chunk embeddings pay off on
  long documents whose chunks lose context when embedded independently; the current corpus
  is single-paragraph documents. Revisit when long-form documents land.
- **LLM-as-reranker (RankGPT, Sun et al., EMNLP 2023)**: listwise quality, but
  per-query LLM latency in the *rerank* stage (the hot path) and prompt-order sensitivity
  make it a poor fit while generation runs on a local CPU model.
- **SPLADE-style learned sparse (Formal et al., 2021)**: subsumed here — M3's sparse head
  provides learned lexical weights without another model.

## Consequences

- **+** The retrieval stack now demonstrates *technique literacy with receipts*: each
  component cites its paper and carries a measured number under the corrected doc-level
  metric.
- **+** `rerank=m3` gives a one-model deployment option (drop the cross-encoder from
  memory) with a measured quality delta to reason about.
- **+** HyDE closes the cross-lingual demo loop for free: a Korean question becomes an
  English hypothetical passage which lands in the English corpus region of bge-m3 space.
- **−** HyDE adds seconds of LLM latency per query (CPU 3B locally) — mode, not default.
- **−** M3 sparse/ColBERT heads add sidecar compute per rerank call (~comparable to the
  cross-encoder pass it replaces).
- **−** Two more surfaces (`rerank=`, `mode=hyde`) to keep honest in eval — accepted,
  that's what the sweep is for.

## Validation

- `SearchServiceHydeTest`: the *hypothetical passage* is what gets embedded (not the
  question); LLM failure and blank generation both fall back to query-vector search with
  the fallback counter incremented.
- Sidecar `/score_m3` returns per-mode sub-scores; combined ranking exercised end-to-end
  by the live `hybrid-m3` sweep.
- Full 5-mode sweep measured on the running stack — table in the README; CI gate
  unchanged and green.
