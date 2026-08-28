# ADR 0015 — The LLM judge is a measuring instrument, and it was never calibrated

Status: accepted · Extends [ADR 0004](0004-groundedness-guardrail.md)

## Context

[ADR 0011](0011-statistical-inference-eval.md) put intervals on every retrieval metric,
[ADR 0012](0012-anytime-valid-evaluation.md) made those intervals safe to watch, and
[ADR 0013](0013-conformal-risk-control.md) turned the serving thresholds into certificates.
All three assume the labels are true.

For retrieval they are: `eval/gold.jsonl` was written by a person. For answer quality they
are not. The README's

> groundedness (avg judge score) **0.81**

is a CHEAP-tier model's opinion of answers produced by the system it belongs to. If that
judge is optimistic by eight points, the published number is wrong by eight points, and
nothing anywhere in the pipeline would notice — the interval machinery would faithfully
report a tight interval around the wrong value.

This is not a local problem. Every RAG system in production grades itself with an LLM, and
at any real scale the *relevance* labels behind a gold set are model-written too — which is
an open argument in IR right now (Faggioli et al., 2023, on LLM-based relevance judgment;
Thomas et al., 2024, on LLM searcher-preference prediction). The two usual answers are both
bad: quote the judge and pretend a biased estimate is a measurement, or hand-label
everything and stop at a few dozen items.

## Decision

Adopt **prediction-powered inference** (Angelopoulos, Bates, Fannjiang, Jordan & Zrnic,
*Science*, 2023; power-tuned as PPI++, Angelopoulos, Bates & Zrnic, 2023), in `eval/ppi.py`.

Hand-label a small sample, let the judge score everything, and combine:

```
theta = mean(hand labels) + lambda * ( mean_unlabelled(judge) - mean_labelled(judge) )
```

The bracket is the judge's **bias, measured on the labelled sample** and subtracted. Two
properties follow, and they are the reason this is worth the code:

**Validity does not depend on the judge being good.** The bias term is estimated from data,
so no assumption about the model is required. A wildly miscalibrated judge produces a wider
interval, never an invalid one.

**A useless judge costs nothing.** `lambda` is tuned to minimise variance:

```
lambda* = Cov(Y, f) / ( Var(f) * (1 + n/N) )
```

At `lambda = 0` the estimator *is* the hand-label mean. So a judge predicting noise is
ignored automatically, and the result is never worse than not having used it. `lambda = 1`
recovers the original untuned PPI estimator.

### Measured

`ppi_experiment.py`, 1,500 independent repetitions of the whole label-and-estimate cycle per
row. 50 hand labels, 2,050 judge-scored items, estimating a quantity whose true value is
known exactly:

| judge behaviour | coverage: judge only | hand labels only | **PPI** |
|---|:---:|:---:|:---:|
| flattering, accurate | **0.0%** | 94.0% | 94.3% |
| flattering, noisy | **0.0%** | 94.2% | 93.5% |
| unbiased, noisy | 14.5% | 94.7% | 93.7% |
| harsh, accurate | **0.0%** | 94.4% | 94.4% |
| uninformative | **0.0%** | 94.8% | 94.3% |

*How often each method's published 95% interval actually contained the truth.* Averaging
thousands of judge scores does not produce a 95% interval — it produces a very narrow
interval around whatever the judge believes, and when the judge is biased it is essentially
never right. Note the "unbiased" row still fails: scores clipped to `[0, 1]` acquire a bias
near the boundary even when the noise is symmetric, which is precisely the kind of thing
nobody checks.

| judge behaviour | width, labels only | width, PPI | narrower by | λ | effective labels (from 50) |
|---|:---:|:---:|:---:|:---:|:---:|
| flattering, accurate | 0.0884 | 0.0291 | **67%** | 0.92 | **474** |
| flattering, noisy | 0.0882 | 0.0685 | 22% | 0.46 | 84 |
| unbiased, noisy | 0.0881 | 0.0683 | 22% | 0.43 | 85 |
| harsh, accurate | 0.0883 | 0.0293 | **67%** | 0.89 | **466** |
| uninformative | 0.0883 | 0.0879 | 1% | 0.05 | 51 |

**Fifty hand labels plus a good judge buy the precision of four hundred and seventy-four
hand labels.** A bad judge costs one percent of interval width — the "uninformative" row is
the safety property working, not a failure.

### In the pipeline

`run_qa_eval.py --human-labels judge-labels.jsonl` grades the same 0 / 0.5 / 1 scale the
judge uses, and prints four things instead of one:

```
judge alone      0.886   (no validity — the judge's opinion of its own system)
hand labels only 0.625   [0.156, 1.094]  n=4
PPI              0.637   [0.341, 0.934]  lambda=1.00
judge bias       +0.250  (optimistic)
effective labels 10      (from 4 actually written)
```

Fewer than two hand labels, or nothing left unlabelled, and it refuses rather than
producing something shaped like a guarantee.

The same estimator applies unchanged to LLM-written *relevance* labels, which is how a gold
set gets past the few-hundred-query ceiling [ADR 0014](0014-beir-benchmark-scale.md) runs
into: hand-judge a hundred query-document pairs, let a model judge ten thousand, and the
resulting nDCG carries a valid interval.

## Consequences

**The repository's own groundedness number is now labelled as what it is.** No hand labels
exist for it yet, so `0.81` stays in the README as the judge's opinion, explicitly, with the
machinery and the file format ready for the fifty labels that would make it a measurement.
Writing those labels is the remaining work, and it is a couple of hours rather than a
research project.

**Annotation budget becomes a number to argue with.** "Effective labels" converts a narrower
interval into the count of hand labels that would have bought it, which is the form the
question actually arrives in.

**Labelled and unlabelled items must be disjoint** samples of the same population. Scoring
the labelled items twice correlates the two terms and voids the interval; the harness splits
on the label file's membership so this cannot be done by accident.

**The scales must match.** Hand labels use the judge's own 0 / 0.5 / 1 scale, because a
rescaling is indistinguishable from a bias and the estimator would dutifully subtract it.

## Alternatives considered

**Quote the judge and add a caveat.** What was already happening. A sentence saying "judged
by an LLM" does not tell a reader the number is eight points high, and it does not stop the
number from being compared against someone else's.

**Hand-label everything.** Correct, and it stops at a few dozen items. The eight judged
answers behind `0.81` are exactly this ceiling.

**Calibrate the judge — fit a mapping from judge score to human score.** Useful, and a
different guarantee: it needs the calibration to transfer, and it is a point estimate with
no interval. PPI needs the judge to be neither good nor calibrated.

**A stronger judge.** Buys accuracy, not validity. A frontier model grading its own
pipeline's answers is still an unvalidated instrument, and the failure mode is identical.

**Human agreement studies (κ) between judge and annotator.** Reports whether the judge
agrees, not what the true rate is, and does not produce an interval on the quantity anyone
publishes.
