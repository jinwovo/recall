## Anytime-valid evaluation — measured

Simulated evaluation streams, 800 per cell, seeded. Reproduce with `python eval/anytime_experiment.py`.

Peeking starts at query 30 so the fixed-N interval is judged in the regime where it is normally considered valid — counting its degenerate small-sample behaviour would inflate the comparison.

### 1. What peeking costs

Share of streams where the true mean falls outside a nominal **95%** interval at *some* point while it is being watched — which is how a CI log is read.

| metric | queries | fixed-N interval | confidence sequence |
|---|:---:|:---:|:---:|
| Recall@5 (0/1) | 50 | **19.2%** | 0.6% |
| Recall@5 (0/1) | 150 | **29.2%** | 2.2% |
| Recall@5 (0/1) | 300 | **34.0%** | 1.9% |
| reciprocal rank (discrete, skewed) | 50 | **15.6%** | 0.6% |
| reciprocal rank (discrete, skewed) | 150 | **25.5%** | 1.8% |
| reciprocal rank (discrete, skewed) | 300 | **32.8%** | 2.6% |
| nDCG@10 (graded) | 50 | **18.8%** | 0.5% |
| nDCG@10 (graded) | 150 | **25.8%** | 0.9% |
| nDCG@10 (graded) | 300 | **35.1%** | 1.6% |
| groundedness judge (skewed high) | 50 | **14.9%** | 0.2% |
| groundedness judge (skewed high) | 150 | **24.5%** | 0.2% |
| groundedness judge (skewed high) | 300 | **30.0%** | 0.5% |

The fixed-N column is not a bug in the interval; it is the interval being used for something it never promised. The right-hand column is a promise that holds at every stopping time, which is what makes stopping early legitimate.

### 2. What stopping early saves

A gate asking `Recall@5 >= 0.85` with a budget of 300 queries, stopped as soon as the verdict is determined.

| true Recall@5 | verdict | decided | mean stop | queries saved | wrong |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 0.98 | pass | 100% | 54 | **82%** | 0.0% |
| 0.95 | pass | 100% | 89 | **70%** | 0.0% |
| 0.92 | pass | 79% | 172 | **43%** | 0.0% |
| 0.90 | undecided | 44% | 236 | **21%** | 0.1% |
| 0.88 | undecided | 12% | 282 | **6%** | 0.2% |
| 0.85 | at the line | 3% | 293 | **2%** | 1.8% |
| 0.82 | undecided | 7% | 286 | **5%** | 0.1% |
| 0.75 | fail | 71% | 179 | **40%** | 0.0% |
| 0.60 | fail | 100% | 31 | **90%** | 0.0% |

A system at the line is correctly reported `undecided` rather than nudged either way, and spends its whole budget doing so — the one case where there is nothing to save and nothing to claim.
