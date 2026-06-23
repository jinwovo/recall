# ADR 0002 — LLM cost & latency optimization

- **Status:** Accepted
- **Date:** 2026-06-23

## Context

A naive RAG call sends a large retrieved context + system prompt to the most
capable model on every request. That is slow and expensive, and most of the cost
is avoidable. This project's thesis is that **LLM usage is a backend engineering
problem** — so cost and latency are first-class, measured concerns.

## Decision

Four levers, all measured:

### 1. Model tiering
Route by task difficulty (configurable, see `.env`):
- `claude-haiku-4-5` — query rewriting, intent classification, cheap pre/post steps.
- `claude-sonnet-4-6` — routine answers.
- `claude-opus-4-8` — hard / low-confidence answers (default for final answers).

### 2. Prompt caching
Keep a **stable prefix**: frozen system prompt + (where reused) retrieved-context
block carry a `cache_control: {type: "ephemeral"}` breakpoint. Cache reads cost
≈ 0.1× input; writes ≈ 1.25×. **Verify** with `usage.cache_read_input_tokens` — a
zero across repeated requests means a silent invalidator (timestamps, unsorted
JSON, varying tool set) and is treated as a bug.

### 3. Semantic cache
Embed the query, look it up in Redis by cosine similarity
(`SEMANTIC_CACHE_THRESHOLD`, default 0.95). On hit, return the cached answer and
skip the LLM entirely. Near-duplicate questions are common in real traffic.

### 4. Batch API for ingestion enrichment
Chunk summaries / metadata generated at ingestion are **not latency-sensitive** →
Claude Message Batches API at **50%** off.

### Latency
Stream answers over **SSE** and measure **TTFT** separately from total latency.
Use **token counting** (`messages.count_tokens`) for pre-flight cost estimates and
to guard `max_tokens`.

## Consequences

- **+** `$/query` and p95 latency become dashboards, not guesses; README shows
  Before/After (e.g. tiering + caching → N% cost reduction).
- **+** Each lever is independently toggleable and independently measurable.
- **−** More moving parts (cache invalidation, tier routing rules) → covered by
  metrics (`recall_llm_cost_usd`, `recall_semantic_cache_hits_total`,
  `recall_prompt_cache_read_tokens`) and tests.
- **−** Semantic cache can serve a stale/wrong answer if the threshold is too low
  → threshold is tuned against the eval set; cache entries carry the source set.

## Notes

- Model IDs are config (`RECALL_MODEL_*`) — never hard-coded — so tiering and
  upgrades are a config change.
- Adaptive thinking is off by default for answer generation (latency); enable per
  route if a task is reasoning-heavy.
