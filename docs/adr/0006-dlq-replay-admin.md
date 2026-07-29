# ADR 0006 — DLQ replay: consumer-group drain on the admin surface

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

ADR 0005 guaranteed that no accepted document is silently lost: poison pills and exhausted
retries land on `recall.ingestion.dlq` with forensic headers. But "not lost" is not
"recovered". The stated replay story — `kafka-console-consumer | kafka-console-producer` —
assumes shell access to the broker, manual JSON surgery, and remembering which records were
already replayed. In practice a DLQ without tooling decays into a write-only graveyard.

The missing operation is the second half of the incident loop: **inspect what failed and
why → fix the fault (deploy, config, sidecar restart) → replay the backlog → verify it
drained.** That loop should be one API call per step, not a runbook.

## Decision

**1. Inspection endpoint.** `GET /api/admin/dlq?limit=` returns the queue **depth** (records
the replay group has not consumed yet) plus a bounded peek from the head of the topic with
the ADR 0005 forensic headers decoded: original topic/offset, exception class and message
(preferring the root-cause header), replay count, payload preview. The peek consumer never
commits — inspecting the DLQ cannot change what will be replayed. Already-replayed records
stay visible (`pending=false`) until retention expires them: the queue doubles as an
incident audit trail.

**2. Replay endpoint.** `POST /api/admin/dlq/replay?max=` drains up to `max` pending records
back onto the ingestion topic under a dedicated consumer group (`recall-dlq-replay`):

- each record is re-published with its key (partition affinity preserved) and broker-acked
  **before** its DLQ offset is committed — crash between publish and commit means
  re-replay, never loss;
- `kafka_dlt-*` forensic headers are stripped on the way out (a re-failure earns fresh
  forensics from the recoverer) while provenance headers are added:
  `recall_dlq-replays` (incrementing count) and `recall_dlq-replay-source` (DLQ
  partition@offset) — both survive the recoverer's header copy, so a record that keeps
  failing carries its full round-trip history;
- progress is committed even when a publish fails mid-drain, so successfully replayed
  records are never replayed twice by the next invocation.

**3. At-least-once, on purpose.** Replay duplicates are absorbed by design: chunk upserts
are idempotent by content hash (ADR 0003), so replaying the same document twice converges
to the same index state. This is the payoff of making idempotency a first-class property —
recovery tooling gets to be simple because the sink is convergent.

**4. Replay is not a retry.** A replayed poison pill fails parsing again and dead-letters
again, immediately, with `replays` incremented — visibly bouncing rather than silently
looping. The contract is *fix first, then replay*; the tooling makes a premature replay
harmless and observable instead of forbidden.

**5. Ops surface.** The frontend `/admin` page shows depth, the forensic table, and a
one-click "Replay pending" button; `recall_ingestion_dlq_replayed_total` joins the
reliability panel in Grafana. Demo scope: the endpoint is as unauthenticated as the rest of
the API — in production it sits behind the gateway's authn/authz like any admin route.

## Alternatives considered

- **Console-tool runbook (status quo)**: zero code, but unauditable, error-prone (manual
  offset bookkeeping), and unusable from the UI. Rejected — the point of the exercise is
  operational maturity.
- **Automatic scheduled replay**: turns the DLQ into a slow retry loop and erases the
  human decision ("is the fault actually fixed?"). Poison pills would bounce forever by
  design. Rejected; the endpoint is deliberately imperative.
- **Selective replay (by key / offset range)**: genuinely useful at scale, but at this
  queue depth a bounded drain covers the incident loop; the poison-pill round-trip is
  harmless. Deferred until there is a real mixed-cause backlog to manage.
- **Kafka Streams / MirrorMaker-style pipeline**: heavy machinery for what is a bounded
  administrative batch operation. Rejected.

## Consequences

- **+** The DLQ story is closed end-to-end: quarantine (ADR 0005) → inspect → fix → replay
  → drained, each step observable (depth, `replayed` counter, Grafana).
- **+** Replay safety is *derived* from existing invariants (idempotent sink, acks=all),
  not from new coordination machinery.
- **−** Replay re-enqueues at the tail of the ingestion topic — original ordering across
  documents is not preserved (irrelevant per-doc: chunk ids are content-addressed).
- **−** A single admin instance serializes replays (`synchronized`); concurrent drains from
  multiple replicas would duplicate work but stay correct. Fine at this scale; leader
  election is the known upgrade path.

## Validation

`DlqReplayIT` (Testcontainers: real Kafka + real MinIO, production error-handler wiring,
scriptable embedder):

- exhausted retries → record visible via `inspect` with decoded forensics, `depth=1`,
  `pending=true`, `replays=0`;
- fault fixed → `replay` returns `replayed=1, remaining=0`; the wire record on the
  ingestion topic carries `recall_dlq-replays: 1` + `recall_dlq-replay-source` and **no**
  `kafka_dlt-*` headers; the document is indexed end-to-end;
- the drain committed: a second `replay` finds nothing; the record remains in the peek as
  `pending=false` history;
- poison pill replayed without a fix → dead-letters again with fresh forensic headers and
  `replays=1` — round-tripped, never lost, never silently retried.
