# ADR 0014 — A benchmark the harness can actually resolve

Status: accepted · Answers the conclusion of [ADR 0011](0011-statistical-inference-eval.md)

## Context

Three ADRs in a row built machinery for measuring retrieval honestly, and all three ended
with the same finding: the gold set they point at cannot resolve anything worth acting on.

- [ADR 0011](0011-statistical-inference-eval.md): `Recall@5 = 1.00` on ten queries is
  `[0.69, 1.00]`, and the headline +0.30 lift over BM25 has a p-value floor of 0.25 — no
  outcome of that comparison could have been significant.
- [ADR 0012](0012-anytime-valid-evaluation.md): a sequential gate saves 70–90% of a query
  budget. On ten queries there is no budget to save.
- [ADR 0013](0013-conformal-risk-control.md): at `alpha = 0.02` the minimum calibration set
  is 49 queries before a coverage guarantee exists at all, and a useful risk bound wants
  four figures.

`power_report.py` prices it precisely: resolving a 0.02 `MRR@10` move at the per-query
spread this corpus produces needs on the order of a thousand queries. The 24-document,
10-query set was the right size to prove a pipeline works end to end and is the wrong size
for every claim built on top of it.

There is a second problem. The corpus is hand-written for this repository, so its numbers
are comparable to nothing. "Hybrid reaches `nDCG@10 = 0.96`" is unfalsifiable in a useful
sense: no reader can tell whether that is good.

## Decision

Fetch and evaluate on **BEIR** (Thakur, Reimers, Rücklé, Srivastava & Gurevych, NeurIPS
2021 Datasets & Benchmarks), the retrieval benchmark suite the field reports against.

`eval/beir.py` converts a BEIR dataset into this repo's corpus and gold format, so
everything downstream — ingestion, the eval harness, the sequential gate, the calibrator —
works unchanged:

```bash
make beir                                  # SciFact: 5,183 documents, 300 queries
make beir-eval                             # ingest through the real pipeline, then evaluate
make calibrate GOLD=beir-scifact/gold.jsonl
```

### SciFact is the default, for one specific reason

Its relevance judgements are **binary**. This repository computes binary-gain metrics, so
on SciFact its `nDCG@10` means the same thing as a published `nDCG@10` and the comparison
is real. On a graded dataset the judgements must be binarised, and the resulting nDCG is
*not* comparable to published numbers — `beir.py` prints that warning on every graded
dataset rather than letting a reader assume otherwise. Recall and MRR are unaffected.

SciFact is also small enough to index on a laptop, and scientific claim verification is a
realistic shape for a technical knowledge base.

`--list` shows the catalog: nfcorpus, fiqa, arguana, scidocs, trec-covid, each with its
size and the one caveat that matters for it.

### Standard library only

Corpus and queries come from the Hugging Face datasets-server as JSON, relevance judgements
from the qrels repositories as TSV. No `datasets`, no pyarrow, no BEIR install — the same
constraint the rest of the harness runs under, so the composite action stays self-contained.

Paging 5,000 documents is fifty-odd requests and the endpoint rate-limits partway through,
so 429 gets its own long backoff and progress is checkpointed after every page. Losing forty
completed pages to the fifty-first is the difference between a tool people run and one they
run once.

### Conversions that would otherwise be silent

Three things get dropped, and all three are counted and reported:

- **judgements pointing outside the corpus** — kept queries lose them, and a query left with
  none is dropped entirely rather than scoring zero for a reason unrelated to retrieval;
- **empty documents** — nothing to index;
- **queries never judged**.

`--max-queries` takes an **evenly spaced** subset, not a head slice: qrels files are ordered
by query id, and the first N carries whatever bias that ordering has. A subset changes
nothing about the corpus, so retrieval stays as hard as the full benchmark — but the metrics
carry the wider interval of the smaller sample, and the tool says so.

### The design analysis comes with the download

After fetching, `beir.py` prints what the benchmark can resolve. For SciFact at 300 queries:

```
smallest attainable p   9.82e-91   (against 2.0e-01 on a 10-query set)
dMRR@10 = 0.02 at sd 0.20 needs   785 queries -> short by 485
dMRR@10 = 0.02 at sd 0.30 needs 1,766 queries -> short by 1,466
```

Thirty times the resolution, and still short of settling a two-point MRR move. That is worth
printing rather than discovering later — the honest summary is that 300 queries resolves
*large* differences decisively and small ones not at all, and the tuning loop's guards
([ADR 0011](0011-statistical-inference-eval.md)) will keep saying so.

## Consequences

**The numbers become comparable.** SciFact `nDCG@10` can be read against the published
BEIR table instead of against nothing.

**The tooling starts paying for itself.** The sequential gate has a budget to save; the
calibrator has enough queries to certify; the tuner's held-out split is large enough to mean
something.

**The 10-query set stays** as the fast local loop and the CI smoke gate. It is the right
size for "does the pipeline work end to end" and is now labelled as that rather than as
evidence.

**CI does not fetch BEIR by default.** Indexing 5,183 documents through Kafka and bge-m3 on
a GitHub runner is minutes of embedding, not seconds. The full benchmark is a
`workflow_dispatch` and a nightly, and the per-PR gate keeps the small set.

**Downloads are gitignored.** The SciFact corpus alone is 8 MB and is reproducible with one
command.

## Alternatives considered

**Write a larger hand-made gold set.** Hundreds of queries with judgements is weeks of work,
and the result is still comparable to nothing.

**MTEB.** Broader and mostly embedding-model evaluation; BEIR is the retrieval-system
benchmark and is a subset of it.

**Use the HF `datasets` library.** One line instead of a paging loop, and it puts pyarrow
and a large dependency tree in front of a tool whose entire distribution story is that it
needs nothing installed.

**Subsample the corpus to make it fast.** Removing distractors makes retrieval artificially
easy and the scores meaningless. `--max-queries` subsets the *queries*, which costs interval
width honestly and leaves the retrieval problem intact.

**Report on trec-covid for the headline.** Deeply judged and only 50 queries — the
p-value floor is excellent per query and the sample is too small, which is the exact mistake
this ADR exists to stop repeating.
