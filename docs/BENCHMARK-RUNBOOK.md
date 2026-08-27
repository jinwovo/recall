# Running the BEIR benchmark

The harness in [`eval/beir.py`](../eval/beir.py) is finished and tested; what it needs is a
machine willing to spend the CPU. This is the runbook, with the costs measured rather than
guessed, so the next person can decide whether their box is the right one before starting.

## What it actually costs

Measured on a 10-core laptop CPU (Windows, Docker Desktop, 7.5 GB to the VM), bge-m3 and
bge-reranker-v2-m3 on CPU:

| stage | measured | notes |
|---|---|---|
| indexing | **~15 documents/minute** | 5,183 SciFact documents ⇒ **~6 hours** |
| sidecar during indexing | 786% CPU, host at 100% | genuinely compute-bound, not misconfigured |
| per-document embed | 3.9 s median | one document, one or two chunks |
| `mode=bm25` search | 4.9 s | *while indexing was running* |
| `mode=vector` search | 2.0 s | *while indexing was running* |
| `mode=hybrid` search | 120–186 s | *while indexing was running* — see below |

**The hybrid figure is contention, not the cost of reranking.** It did not scale with
rerank depth — 186 s at `candidates=5` against 157 s at `candidates=10` — which is the
signature of a starved process, not of more work. On an idle machine expect seconds. Do not
quote these search numbers as latency; they exist here only to say "do not evaluate while
you index."

The honest summary: **this is an overnight job on a laptop CPU.** A GPU turns the indexing
into minutes. More cores help roughly linearly until memory bandwidth binds.

## Before you start, on a bigger box

Three settings were tuned against a 10-core machine and should move with the hardware.

```bash
# Listener threads. Capped by the topic's partition count (3 by default), so raise both
# together — the ingestion topic is created by KafkaTopicConfig, or ALTER it in place:
#   kafka-topics.sh --bootstrap-server localhost:9092 \
#       --alter --topic recall.ingestion --partitions 8
export KAFKA_CONSUMER_CONCURRENCY=3
```

```yaml
# docker-compose.hostdev.yml — threads *per request*, not per machine.
# Torch defaults to one thread per core, so N in-flight requests each spawn a full-width
# pool and the cores thrash. Measured: raising concurrency 1 -> 3 without this made
# indexing *slower* (17 -> 10 docs/min) while CPU went 207% -> 617%.
# Rule of thumb: concurrency x OMP_NUM_THREADS <= physical cores.
OMP_NUM_THREADS: "3"
```

```yaml
# application.yml — leave these alone unless a record takes longer than ~60s.
# A poll batch must finish inside max.poll.interval.ms or the broker evicts the consumer,
# the group rebalances, and everything since the last commit is redelivered. See the
# amendment in ADR 0003: at the 500-record default this pipeline reprocesses forever while
# every log line and metric says it is healthy.
max-poll-records: 10
max.poll.interval.ms: 600000
```

## The run

```bash
# 1. Fetch and convert. Cached and resumable; the design analysis prints at the end.
make beir                                    # SciFact: 5,183 docs, 300 queries, binary qrels

# 2. Infrastructure in compose, backend on the host.
docker compose -f docker-compose.yml -f docker-compose.hostdev.yml up -d \
    elasticsearch redis postgres kafka minio embedding-service
cd backend && REDIS_PORT=6383 POSTGRES_PORT=5437 KAFKA_CONSUMER_CONCURRENCY=3 ./gradlew bootRun

# 3. Index. Hours. Watch the *committed offset*, not the log:
python scripts/seed_corpus.py eval/beir-scifact/corpus.jsonl --timeout 36000
docker exec recall-kafka /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 --describe --group recall-ingestion
```

The log printing `indexed N chunks for doc …` proves nothing on its own — that is exactly
what the redelivery loop looks like. Progress is `CURRENT-OFFSET` advancing.

```bash
# 4. Recover anything the pipeline quarantined. A sidecar restart mid-run dead-letters the
#    in-flight documents, and a benchmark measured on an incomplete corpus is not the
#    benchmark. This is what the DLQ replay of ADR 0006 is for.
curl -s localhost:18080/api/admin/dlq?limit=20
curl -sX POST localhost:18080/api/admin/dlq/replay?max=100

# 5. Confirm the corpus is complete before measuring anything.
curl -s 'localhost:9200/recall-docs/_search?size=0' -H 'Content-Type: application/json' \
  -d '{"aggs":{"d":{"cardinality":{"field":"docId","precision_threshold":40000}}}}'
#    -> must read 5183

# 6. Evaluate. Nothing else running: the search path shares the embedding sidecar with
#    ingestion, and a contended measurement is worthless.
make beir-eval
```

## Once it finishes

```bash
# The gate the whole benchmark exists to make meaningful:
cd eval && python run_eval.py beir-scifact/gold.jsonl --gate --gate-policy sequential

# And the certificates, which need a few hundred queries before they can certify anything:
python calibrate.py beir-scifact/gold.jsonl --json ../calibration.json
```

Paste `beir-summary.md` into the README's results section. SciFact's judgements are binary,
so its `nDCG@10` is directly comparable to the published BEIR table — that is why it is the
default and why a graded dataset would need the caveat `beir.py` prints.

`calibrate.py` writes the `recall.rag.conformal` block to paste into `application.yml`; a
threshold is valid only for the alpha, temperature, corpus and reranker it was calibrated
on, so it has to be redone per benchmark.

## Why the sequential gate matters here

At benchmark scale a query stops being free. `--gate-policy sequential` stops the moment the
verdict is settled and leaves 70–90% of the query budget unspent
([ADR 0012](adr/0012-anytime-valid-evaluation.md)) — the difference between an evaluation
you run on every pull request and one you run when you remember to.
