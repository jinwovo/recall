# ADR 0005 — Ingestion reliability: retries, dead-letter queue, claim-check raw storage

- **Status:** Accepted
- **Date:** 2026-07-13

## Context

ADR 0003 made ingestion async and idempotent, but the failure path was a stub: the Kafka
consumer wrapped the whole pipeline in `catch (Exception) { log.error(...) }`. Any failure —
the embedding sidecar restarting, ES briefly unavailable, a malformed event — was logged and
**silently dropped**. The producer side had the mirror-image gap: `KafkaTemplate.send()` was
fire-and-forget, so a 202 response didn't actually promise the event reached the broker, and
the "persist raw document to MinIO" line in ADR 0003 was a TODO. Kafka also caps message size
(1 MB default), so "just inline the document" stops working exactly when documents get
interesting.

An ingestion pipeline that can lose accepted documents undermines the headline claim of
ADR 0003 ("no duplicates, no loss" — the integration test only proved the *no duplicates*
half).

## Decision

**1. Typed failure classification.** The consumer no longer catches anything. Failures are
split into two kinds:

- `NonRetryableIngestionException` — poison pills: malformed JSON, missing `docId`, an event
  with neither inline content nor an object reference. Redelivery can never succeed, so no
  retries are attempted.
- Everything else — presumed transient (sidecar/ES/MinIO connectivity, embedding count
  mismatch): retried in place with exponential backoff
  (`KAFKA_RETRY_MAX_ATTEMPTS`, default 3; `KAFKA_RETRY_BACKOFF_MS`, default 1000 ms, ×2 per
  attempt), via Spring Kafka's `DefaultErrorHandler`.

**2. Dead-letter queue.** Poison pills and exhausted retries are published to
`recall.ingestion.dlq` by a `DeadLetterPublishingRecoverer`, preserving the original payload
plus forensic headers (`kafka_dlt-original-topic/-partition/-offset`, exception class,
message, stacktrace). Replay is a plain `kafka-console-consumer | kafka-console-producer`
away — no record is ever dropped. In-place blocking retry (vs. non-blocking retry topics) is
a deliberate trade-off: it briefly delays that partition, but ingestion is a background path
where ordering-per-doc and simplicity matter more than consumer throughput under failure.

**3. Broker-acked accept path.** `enqueue()` now blocks (off the event loop, bounded 10 s)
on the `send()` future with `acks=all`. A 202 means the event is durably in Kafka, not "it
probably left the building".

**4. Raw-document archive + claim check.** Every accepted document is stored in MinIO
(`recall-raw` bucket, `docs/<docId>/raw.txt`) *before* the event is published:

- Documents ≤ `INGEST_INLINE_MAX_BYTES` (64 KB default) still carry inline content (one hop
  fewer for the common case) — for them the archive is best-effort: if MinIO is down, the
  doc proceeds inline and `recall_ingestion_raw_store_failures_total` increments.
- Larger documents travel as an `objectKey` reference (the **claim-check pattern**); the
  consumer fetches the content back from MinIO. For them the archive write is mandatory —
  a failure rejects the ingest rather than accepting a document that cannot be delivered.
- The archive doubles as the re-indexing source of truth (re-embed with a new model without
  re-uploading) — the MinIO line item from ADR 0003, now real.

**5. Observability.** New counters: `recall_ingestion_retries_total`,
`recall_ingestion_dlq_total`, `recall_ingestion_dlq_publish_failures_total`,
`recall_ingestion_raw_store_failures_total`, all on the Grafana overview dashboard. A DLQ
rate above zero is a pageable signal, not a log line.

## Alternatives considered

- **Non-blocking retries (retry topics / `@RetryableTopic`)**: keeps the partition flowing
  during backoff, at the cost of N extra topics and out-of-order redelivery. Unnecessary at
  this throughput; revisit if ingestion becomes latency-sensitive.
- **Store nothing / inline everything**: hits the Kafka message cap and couples document
  size to broker tuning (`max.request.size` creep). Rejected.
- **Store everything / inline nothing**: simplest wire format, but adds a mandatory MinIO
  round trip (and a hard MinIO availability dependency) to every small doc. The threshold
  keeps the archive's durability benefits without making it a single point of failure for
  the common case.
- **DB-backed outbox instead of DLQ**: heavier machinery than needed — the DLQ already
  gives at-least-once with idempotent upserts (ADR 0003) absorbing the duplicates.

## Consequences

- **+** "202 ⇒ durable": raw doc archived (or inlined) AND broker-acked before the response.
- **+** Transient infra blips self-heal (backoff retries); permanent failures are inspectable
  and replayable on the DLQ with full forensics, instead of vanishing into logs.
- **+** Document size is decoupled from Kafka limits (claim check).
- **−** A failing record blocks its partition for the backoff window (~7 s at defaults) —
  acceptable for a background pipeline, see alternatives.
- **−** MinIO joins the accept path (hard dependency only for large docs; degraded mode for
  small ones) — mitigated by the fail-open-with-metric design.

## Validation

`IngestionReliabilityIT` (Testcontainers: real Kafka + real MinIO, production wiring, mocked
embedder/index so failures are scriptable):

- malformed payload → DLQ immediately, **zero** embedding calls (no retries burned);
- event with neither content nor objectKey → DLQ immediately;
- embedder fails twice then recovers → exactly 3 delivery attempts, document indexed, DLQ
  stays empty;
- embedder permanently down → exactly 1 + 3 attempts, then DLQ record with original payload
  and forensic headers;
- document far above the inline threshold → wire event carries `objectKey` and **no** inline
  content; MinIO object is byte-identical; consumer resolves and indexes the full text;
- small document → travels inline *and* is archived.
