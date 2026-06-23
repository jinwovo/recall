# ADR 0003 — Async, idempotent ingestion via Kafka

- **Status:** Accepted
- **Date:** 2026-06-23

## Context

Ingestion (parse → chunk → enrich → embed → index) is slow and bursty: a single
upload can fan out to hundreds of chunks, each needing an embedding call. Doing
this synchronously in the request thread blocks the API and risks timeouts.
Re-uploads and retries must not create duplicates or lose chunks.

## Decision

- The API persists the raw document (MinIO) and emits an event to a **Kafka**
  topic (`recall.ingestion`); the HTTP request returns immediately.
- **Ingestion workers** consume the topic and run the pipeline. Embedding calls
  to the sidecar are **batched**.
- **Idempotency:** every chunk is keyed by `content_hash = sha256(doc_id +
  chunk_index + content)` and **upserted** into Elasticsearch. Re-processing the
  same document (retry, duplicate event, concurrent ingestion of an updated
  version) converges to the same index state — no duplicates, no loss.
- **Incremental updates:** on re-ingestion, chunks whose hash is unchanged are
  no-ops; removed chunks are tombstoned by `doc_id` + version.

This also lets ingestion enrichment use the **Batch API** (ADR 0002) since it is
off the request path.

## Consequences

- **+** API stays responsive under bursty ingestion; workers scale independently.
- **+** Idempotency makes retries safe — the core of the "concurrency /
  race-condition" troubleshooting narrative (README).
- **+** Kafka is actually implemented here (the sibling `realtime-messaging`
  project wired Kafka but left it on the roadmap).
- **−** Eventual consistency: a just-uploaded doc isn't searchable until indexed
  → surfaced as ingestion status in the admin dashboard.
- **−** Operational surface (Kafka) added → justified by the async requirement and
  covered by Testcontainers integration tests.

## Validation

- Integration test: fire N concurrent ingestions of the same doc → assert exactly
  the expected chunk count in ES (no dup/loss).
- Metric: `recall_ingestion_docs_total`, consumer lag, indexing throughput (docs/s).
