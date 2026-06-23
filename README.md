# Recall — AI Hybrid Search & RAG QA Platform

![CI](https://github.com/jinwovo/recall/actions/workflows/ci.yml/badge.svg)

> Hybrid (BM25 + dense vector) search and grounded RAG question-answering over a
> technical knowledge base — built as a **backend engineering problem**, not a
> thin LLM wrapper. The hard parts are retrieval quality, latency budgets, LLM
> cost control, and groundedness — all measured, not asserted.

**Status:** 🚧 Active development — see [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) for the roadmap.

한국어 요약은 [아래](#한국어-요약)에 있습니다.

---

## Why this project

Most "AI search" demos call an LLM once and stop. This one treats the LLM as one
component inside a system with real engineering constraints:

| Engineering challenge | What it demonstrates | Tracked metric |
|---|---|---|
| **Hybrid retrieval** (BM25 + vector + RRF + reranking) | Search/IR depth, fusion tuning | Recall@10, MRR@10, nDCG@10 |
| **RAG with native citations + hallucination guardrails** | Grounded generation, "I don't know" handling | groundedness %, citation coverage |
| **LLM cost & latency optimization** | Model tiering, prompt caching, semantic cache, batch | $/query, cache hit rate, % cost saved |
| **Multilingual KO/EN** | Korean morphology (Nori) + multilingual embeddings | per-language recall, cross-lingual hit rate |
| **Async ingestion pipeline** | Concurrency, idempotency, incremental indexing | docs/s, zero dup/loss |
| **Eval-driven development** | Measured iteration, regression gates in CI | eval trend, CI gate |

### Cost engineering (the core backend story)

- **Model tiering** — `claude-haiku-4-5` for query rewriting/classification,
  `claude-sonnet-4-6` for routine answers, `claude-opus-4-8` for hard ones.
- **Prompt caching** — stable system prompt + retrieved context prefix cached
  (cache reads ≈ 0.1× input cost); verified via `cache_read_input_tokens`.
- **Semantic cache** — query embedding → Redis cosine match → skip the LLM call
  for near-duplicate questions.
- **Batch API** — chunk summarization / metadata enrichment at ingestion runs at
  50% off.
- **Token counting** pre-flight cost estimates + **SSE streaming** for low TTFT.

---

## Architecture

```
   Next.js (TS)            Spring Boot BFF / API Gateway (Java 21)
   - search / QA UI   ───▶ REST + SSE (streaming)
   - source highlight      ├─ Search Service   → Elasticsearch (BM25+Nori + kNN) → RRF → reranker
   - admin dashboard       ├─ RAG Service      → Claude (Opus 4.8 / Sonnet 4.6 / Haiku 4.5)
                           │                     prompt caching · SSE · native citations · guardrail
                           ├─ Ingestion        → Kafka → chunk/embed workers
                           ├─ Semantic Cache   → Redis (cosine)
                           └─ Eval / Cost      → Prometheus + Postgres
                                  │
   Elasticsearch   Redis   Postgres   MinIO/S3   Python sidecar (FastAPI)
   (docs+BM25+vec) (cache) (meta/logs (raw docs)  ├─ bge-m3 embeddings (KO/EN)
                           /cost/eval)             └─ bge-reranker-v2-m3
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/adr/`](docs/adr/) for the design decisions.

## Tech stack

- **Backend:** Java 21, Spring Boot 4.1, Spring WebFlux (SSE)
- **LLM:** Claude via `com.anthropic:anthropic-java` (`claude-opus-4-8` default)
- **Search/vector:** Elasticsearch (Nori analyzer + `dense_vector` kNN)
- **Embedding/rerank:** `BAAI/bge-m3` + `BAAI/bge-reranker-v2-m3` (Python FastAPI sidecar)
- **Messaging:** Kafka (async ingestion)
- **Stores:** Redis (semantic cache / locks / rate limit), PostgreSQL (metadata, logs, cost ledger, eval), MinIO/S3 (raw docs)
- **Frontend:** Next.js, TypeScript, Tailwind, shadcn/ui
- **Observability:** Micrometer + Prometheus + Grafana (+ token/cost dashboard)
- **Quality/infra:** Testcontainers, k6, GitHub Actions, Docker Compose → k8s (Helm)

## Quickstart

```bash
cp .env.example .env          # set ANTHROPIC_API_KEY
docker compose up -d          # ES (Nori), Redis, Postgres, Kafka, MinIO, sidecar, Prometheus, Grafana
cd backend && gradle wrapper && ./gradlew bootRun   # `gradle wrapper` once to generate gradlew
cd frontend && npm install && npm run dev            # http://localhost:3000

./scripts/seed.sh             # ingest sample docs
cd eval && python run_eval.py queries.example.jsonl  # bm25 vs vector vs hybrid comparison table
```

Search exposes a mode for the eval sweep: `GET /api/search?q=...&mode=bm25|vector|hybrid`
(default `hybrid`). A provisioned Grafana dashboard (**Recall — Overview**) shows token usage by
model, semantic-cache hits, retrieval p95 by mode, and ingestion throughput.

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8080 |
| Elasticsearch | http://localhost:9200 |
| Embedding sidecar | http://localhost:8000/docs |
| Grafana | http://localhost:3001 |
| Prometheus | http://localhost:9090 |
| MinIO console | http://localhost:9001 |

## Target metrics

- **Retrieval:** Recall@10 / MRR@10 / nDCG@10 — hybrid vs BM25-only vs vector-only.
- **RAG:** groundedness %, citation coverage, hallucination rate.
- **Latency:** end-to-end p50/p95, TTFT (first SSE token), retrieval latency.
- **Cost:** $/query, prompt-cache hit rate, cost reduction from tiering+caching.
- **Throughput:** ingestion docs/s, sustained QPS, semantic-cache hit rate.

---

## 한국어 요약

**Recall** 은 기술 지식베이스를 대상으로 한 **하이브리드 검색(BM25+벡터) + RAG 질의응답**
플랫폼입니다. "LLM API 한 번 호출"이 아니라 **검색 정합성·지연 예산·LLM 비용·근거(citation)**
를 설계하고 **숫자로 측정**하는 백엔드 엔지니어링 프로젝트입니다.

핵심 차별점:
- **하이브리드 검색** — BM25(Nori 형태소) + 벡터 + RRF 융합 + 리랭킹, Recall@10/MRR로 검증
- **RAG + 근거 인용 + 환각 가드레일** — Claude native citations, "모름" fallback
- **LLM 비용 최적화** — 모델 티어링 / 프롬프트 캐싱 / 시맨틱 캐시 / Batch API → `$/query` 절감 수치화
- **멀티링궐(KO/EN)** · **비동기 색인(Kafka)** · **평가 주도 개발(eval CI)**

자세한 계획은 [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) 참고.

## Project status

Scaffold + core paths implemented; being built toward the milestones in
[`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md).

**Implemented**
- Hybrid retrieval (BM25 + kNN + RRF + rerank) behind a `DocumentIndex` interface
- Search modes (`?mode=bm25|vector|hybrid`) + eval harness sweep (Recall@K / MRR / nDCG)
- RAG over SSE with `[n]` citations, semantic cache, model tiering, prompt caching
- Groundedness floor (no context → "I don't know", no LLM call)
- Async ingestion (Kafka) with content-hash idempotency
- Query log (Postgres/JPA), Micrometer metrics + provisioned Grafana dashboard
- Next.js UI with streamed answers + clickable citation → source highlight

**Planned / TODO**
- Claude **native citations** (replace `[n]` convention) + post-hoc LLM-judge groundedness
- MinIO raw-doc storage on the ingestion path; dead-letter + retry
- Enable the Testcontainers idempotency IT in CI; eval regression gate
- Helm chart for k8s

> Status note: the backend compiles and unit tests pass in CI, the frontend builds,
> and the sidecar passes syntax checks (see the Actions tab). A full end-to-end run
> against the live infra (ES / Kafka / Redis / Postgres / sidecar) hasn't been
> recorded yet. For local backend runs, generate the Gradle wrapper once with
> `gradle wrapper` in `backend/`.

## License

MIT (portfolio project) — see [`LICENSE`](LICENSE).
