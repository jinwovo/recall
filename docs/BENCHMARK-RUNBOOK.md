# Running the BEIR benchmark

The harness in [`eval/beir.py`](../eval/beir.py) is finished and tested; what it needs is a
machine willing to spend the CPU. This is the runbook, with the costs measured rather than
guessed, so the next person can decide whether their box is the right one before starting.

## What it actually costs

Two machines have run this now. The second run corrected the first on three points, so both
are here — the disagreements are the useful part.

| | box A (first run) | box B (this run) |
|---|---|---|
| CPU | 10-core laptop | AMD Ryzen 7 H 255, 8 physical / 16 logical |
| RAM to the Docker VM | 7.5 GB | 12.55 GB |
| indexing | ~15 docs/min | **24.5 docs/min** |
| 5,183 documents | ~6 hours | **3 h 40 m** |

Fewer cores and it still indexed faster; the VM's memory is the only measured difference
large enough to explain it. Do not read the per-box numbers as a law — read the *shape*:
indexing is compute-bound on the sidecar, and search is dominated by reranking.

**Search latency, measured on box B with the corpus fully indexed and nothing else
running:**

| stage | idle | notes |
|---|---|---|
| `mode=bm25` | **1.21 s** | |
| `mode=vector` | **0.49 s** | |
| `mode=hybrid` | **161–164 s** | cross-encoder over `candidates=50` |
| `mode=hybrid&rerank=m3` | **87.9 s** | ADR 0008's cheaper strategy |
| per-document embed | 3.9 s median | one document, one or two chunks |

### Correction: the hybrid figure is reranking, not contention

An earlier version of this runbook said the opposite — that 120–186 s hybrid searches were a
starved process, and that an idle machine should "expect seconds." **Both claims were wrong,
and the evidence for them was invalid.**

The argument was that latency did not scale with rerank depth: 186 s at `candidates=5`
against 157 s at `candidates=10`. But `TuningOverrides.CANDIDATES_MIN` is **10**, so
`candidates=5` is clamped to 10 — the two measurements were the same configuration, and
186 vs 157 was noise. Swept *above* the clamp, on an idle box, it is linear:

| candidates | idle latency | per candidate |
|---|---|---|
| 10 | 27.8 s | 2.78 s |
| 25 | 75.0 s | 3.00 s |
| 50 (stock) | 161.2 s | 3.22 s |

Three independent measurements agree on the stock setting: 161–164 s standalone, 161.2 s in
the sweep, 163.6 s sustained across a 300-query evaluation. On a CPU reranker this is simply
what bge-reranker-v2-m3 costs; a GPU changes it, an idle machine does not.

**The consequence is the number that matters for planning:**

```
bm25    300 x ~1.2s  =    6 min
vector  300 x ~0.5s  =  2.5 min
hybrid  300 x  163s  = 13.6 hours
---------------------------------
make beir-eval         ~14 hours    <- an overnight job, not a coffee break
```

## Before you start, on a bigger box

Three settings were tuned against a 10-core machine and should move with the hardware.

```bash
# Listener threads. Capped by the topic's partition count (3 by default), so raise both
# together — the ingestion topic is created by KafkaTopicConfig, or ALTER it in place:
#   kafka-topics.sh --bootstrap-server localhost:9092 \
#       --alter --topic recall.ingestion --partitions 8
export KAFKA_CONSUMER_CONCURRENCY=3
```

**Set this equal to the partition count, not below it.** Kafka gives a partition to exactly
one consumer, so with `concurrency=2` against 3 partitions one thread takes two partitions
and processes them in series. That thread's partitions fall behind, the single-partition
thread finishes early and then idles, and **the tail of the run is single-threaded**.
Measured on box B: throughput fell 27.2 -> 13.9 docs/min the moment the short partition
drained, and the sidecar halved from 493% to 236% CPU. Raising concurrency mid-run restores
parallelism (236% -> 454%) but cannot repay the backlog already piled onto one partition —
that debt is only ever paid serially. The `concurrency x OMP_NUM_THREADS <= physical cores`
rule below is real, but it is the *second* constraint: satisfy the partition count first and
lower `OMP_NUM_THREADS` if the product no longer fits.

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

On Windows Git Bash, prefix every `docker exec` with `MSYS_NO_PATHCONV=1`. MSYS rewrites the
*container's* absolute path into a host path and the call fails with
`stat C:/.../opt/kafka/bin/...: no such file or directory`, which reads like a broken image
rather than a shell artefact.

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
#
#    Do NOT use `make beir-eval` here. That target seeds *and* evaluates, and step 3 has
#    already seeded — so it re-POSTs all 5,183 documents, the barrier passes instantly
#    because the docIds are already in Elasticsearch, and the evaluation then runs against
#    a sidecar busy re-embedding them. What is idempotent is the Elasticsearch upsert, not
#    the embedding: every duplicate still costs its ~3s before the content hash discards it.
#    Measured contaminated vs clean: 0.45 vs 2.4 sidecar embeds/second.
cd eval && RECALL_SEARCH_TIMEOUT=600 python run_eval.py beir-scifact/gold.jsonl --json ../docs/beir-results.json --markdown ../docs/beir-summary.md
```

`RECALL_SEARCH_TIMEOUT` matters at this depth: the default is 180s and a stock
`candidates=50` hybrid query takes ~163s, which is a 17-second margin across 300 queries.

If you already re-seeded, do not wait it out — stop the backend, skip the duplicates, and
re-verify with step 5. If it still reads 5183, everything skipped was a duplicate:

```bash
MSYS_NO_PATHCONV=1 docker exec recall-kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --group recall-ingestion --topic recall.ingestion --reset-offsets --to-latest --execute
```

## Once it finishes

```bash
# The gate the whole benchmark exists to make meaningful:
cd eval && python run_eval.py beir-scifact/gold.jsonl --gate --gate-policy sequential

# And the certificates, which need a few hundred queries before they can certify anything:
python calibrate.py beir-scifact/gold.jsonl --json ../calibration.json
```

`docs/beir-summary.md` is what the README's results section is built from — it is already
filled in for SciFact. Regenerate both together. SciFact's judgements are binary,
so its `nDCG@10` is directly comparable to the published BEIR table — that is why it is the
default and why a graded dataset would need the caveat `beir.py` prints.

`calibrate.py` writes the `recall.rag.conformal` block to paste into `application.yml`; a
threshold is valid only for the alpha, temperature, corpus and reranker it was calibrated
on, so it has to be redone per benchmark.

**Budget for these separately.** `calibrate.py` issues its own `mode=hybrid` search for every
query in the gold set and needs all three splits, so at SciFact scale it is a second ~14-hour
run, not a postscript to the first. The sequential gate is the cheap one — it evaluates only
`--gate-mode` (hybrid) and stops as soon as the verdict is settled.


## Why the sequential gate matters here

At benchmark scale a query stops being free. `--gate-policy sequential` stops the moment the
verdict is settled and leaves 70–90% of the query budget unspent
([ADR 0012](adr/0012-anytime-valid-evaluation.md)) — the difference between an evaluation
you run on every pull request and one you run when you remember to.
