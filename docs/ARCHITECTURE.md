# Architecture

> Component design, data flow, and the request/index paths. Decisions with
> trade-offs live in [`adr/`](adr/).

## 1. Components

| Component | Responsibility | Tech |
|---|---|---|
| **BFF / API Gateway** | REST + SSE surface for the frontend; orchestration | Spring Boot (WebFlux) |
| **Search Service** | Hybrid retrieval: BM25 (Nori) + dense kNN → RRF fusion → rerank | ES + sidecar |
| **RAG Service** | Prompt assembly, prompt caching, Claude call (tiered), citations, groundedness guard, SSE | anthropic-java |
| **Ingestion Service** | Fetch → chunk → embed → index; async + idempotent | Kafka workers |
| **Semantic Cache** | Query embedding → Redis cosine match → serve cached answer | Redis |
| **Eval / Cost** | Retrieval & RAG metrics, token/cost ledger | Postgres + Micrometer |
| **Embedding sidecar** | `bge-m3` embeddings, `bge-reranker-v2-m3` rerank | Python FastAPI |

## 2. Query path (search + RAG)

```
client ─query─▶ BFF
                 │ 1. (optional) query rewrite/classify  → Claude Haiku
                 │ 2. semantic cache lookup (Redis)       → hit? return
                 │ 3. embed query (sidecar)
                 │ 4. hybrid retrieve (ES):
                 │      - BM25 (Nori analyzer)
                 │      - kNN over dense_vector
                 │      - RRF fuse → top-50
                 │ 5. rerank top-50 → top-K (sidecar cross-encoder)
                 │ 6. assemble prompt (system + cached prefix + context + question)
                 │ 7. Claude (tiered) with native citations, stream via SSE
                 │ 8. groundedness guard → "I don't know" fallback if unsupported
                 │ 9. write cost/eval ledger; populate semantic cache
                 ▼
            SSE stream (answer tokens + citation refs) ─▶ client
```

## 3. Index path (ingestion)

```
upload/crawl ─▶ BFF ─▶ MinIO (raw) ─▶ Kafka topic `recall.ingestion`
                                          │
                                  ingestion worker
                                    - parse (PDF/MD/HTML)
                                    - chunk (token-aware, overlap)
                                    - (batch) metadata/summary enrichment → Claude Batch API
                                    - embed chunks (sidecar, batched)
                                    - upsert into ES (idempotent by content hash)
                                          │
                                       Elasticsearch  (BM25 + dense_vector)
```

Idempotency: each chunk keyed by `sha256(doc_id + chunk_index + content)`; re-ingestion
upserts, so concurrent/duplicate ingestion produces no duplicates or loss. See
[`adr/0003`](adr/0003-async-ingestion-kafka.md).

## 4. Elasticsearch mapping (sketch)

```jsonc
{
  "settings": { "analysis": { "analyzer": { "korean": { "type": "nori" } } } },
  "mappings": {
    "properties": {
      "doc_id":      { "type": "keyword" },
      "chunk_index": { "type": "integer" },
      "content":     { "type": "text", "analyzer": "korean" },   // BM25
      "content_en":  { "type": "text", "analyzer": "english" },
      "embedding":   { "type": "dense_vector", "dims": 1024, "index": true, "similarity": "cosine" },
      "lang":        { "type": "keyword" },
      "source":      { "type": "keyword" },
      "content_hash":{ "type": "keyword" }
    }
  }
}
```

## 5. Cost & latency design

- **Tiering** — Haiku (rewrite/classify) / Sonnet (routine) / Opus (hard). See [`adr/0002`](adr/0002-llm-cost-optimization-strategy.md).
- **Prompt caching** — stable system prompt + retrieved-context prefix carry a
  `cache_control` breakpoint; verify `cache_read_input_tokens > 0`.
- **Semantic cache** — Redis cosine over query embeddings, threshold `SEMANTIC_CACHE_THRESHOLD`.
- **Batch API** — ingestion enrichment (summaries/metadata) at 50% off.
- **SSE** — stream answers; measure TTFT separately from total latency.

## 6. Observability

- Micrometer → Prometheus → Grafana (consistent with `realtime-messaging`).
- Custom metrics: `recall_retrieval_latency`, `recall_llm_tokens_total{model,type}`,
  `recall_llm_cost_usd`, `recall_semantic_cache_hits_total`, `recall_prompt_cache_read_tokens`.
- Eval results persisted to Postgres; surfaced as a Grafana panel + CI gate.

## 7. Deployment

- **Dev:** Docker Compose (this repo).
- **Prod (差별화):** Helm chart for k8s — stateless backend (HPA), ES/Redis/PG as
  managed or statefulsets, sidecar as a separate deployment with GPU/CPU profile.
