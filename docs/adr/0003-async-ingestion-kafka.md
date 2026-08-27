# ADR 0003 — Async, idempotent ingestion via Kafka

- **Status:** Accepted (amended 2026-08-27 — see Amendment)
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

## Amendment (2026-08-27) — what a 5,183-document corpus exposed

Seeding the SciFact benchmark ([ADR 0014](0014-beir-benchmark-scale.md)) was the first time
this pipeline ran against a corpus two orders of magnitude larger than the eval set, and it
broke in two ways that the 24-document corpus could not have surfaced.

**The consumer was reprocessing forever, and every signal said it was fine.** A poll batch
must complete inside `max.poll.interval.ms` or the broker evicts the consumer, the group
rebalances, and everything since the last commit is delivered again. Ingestion embeds each
document through a model, so a record costs seconds rather than microseconds: at the
500-record default a batch needs roughly forty minutes against a five-minute deadline.

The symptom is the worst kind an async pipeline can produce. The log streams
`indexed N chunks for doc …` continuously, the embedding sidecar answers `200 OK`, CPU is
pinned, consumer lag is reported and non-zero — and the committed offset never advances, so
the same prefix is re-embedded indefinitely and the index does not grow. **The idempotency
this ADR is built on is what hides it**: re-upserting by content hash is a no-op, so
correctness is preserved perfectly while throughput is zero. A 24-document corpus fits
inside a single poll batch, commits once, and finishes before the deadline is ever near.

`max-poll-records` is now bounded (10) and `max.poll.interval.ms` raised, so a batch commits
long before eviction. The right diagnostic for "is ingestion progressing" is the **committed
offset**, not the log and not the CPU.

**Concurrency without a thread budget is a pessimisation.** The listener was
single-threaded against a three-partition topic, so raising `consumer-concurrency` looked
like free throughput. Measured on a ten-core host: 17 documents/minute at concurrency 1
became **10 at concurrency 3**, while sidecar CPU went from 207% to 617%. Torch allocates
one thread per core by default, so three in-flight requests each spawned a full-width pool
and the cores thrashed. Pinning `OMP_NUM_THREADS` on the sidecar makes three consumers times
three threads fit the machine instead of fighting over it.

**Still open.** Each document is one `/embed` call carrying its one or two chunks, and a
transformer on CPU is far more efficient at batch 32 than at batch 2. A batch listener would
be the large remaining win, and it trades against the per-record dead-letter semantics of
[ADR 0005](0005-ingestion-reliability-dlq-claim-check.md) — a batch that fails has to be
decomposed to find the poison record, or the whole batch is quarantined together. Not taken
yet; recorded so the next person does not rediscover the ceiling instead of the trade-off.
