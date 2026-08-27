# ADR 0012 — Anytime-valid evaluation: stop when the verdict is decided

Status: accepted · Builds on [ADR 0011](0011-statistical-inference-eval.md)

## Context

Evaluation is sequential. Queries are scored one at a time, results stream into a CI log,
and somebody watches. Every statistical tool applied to it — including the one
[ADR 0011](0011-statistical-inference-eval.md) just added — assumes a single look at a
finished sample. That mismatch costs twice.

**It invalidates the statistics.** A 95% interval promises that *one* look at a *completed*
sample contains the truth 95% of the time. It promises nothing about an interval recomputed
after every query and inspected continuously, which is how anyone actually reads a CI run.
The failure is not subtle. Simulating 800 evaluation streams per cell, with the fixed-N
interval given every benefit of the doubt — inspection starts at n = 30, where the normal
approximation is defensible rather than degenerate:

| metric | queries | fixed-N interval | confidence sequence |
|---|:---:|:---:|:---:|
| Recall@5 (0/1) | 50 → 300 | 19.2% → **34.0%** | 0.6% → 1.9% |
| reciprocal rank (discrete, skewed) | 50 → 300 | 15.6% → **32.8%** | 0.6% → 2.6% |
| nDCG@10 (graded) | 50 → 300 | 18.8% → **35.1%** | 0.5% → 1.6% |
| groundedness judge (skewed high) | 50 → 300 | 14.9% → **30.0%** | 0.2% → 0.5% |

Share of runs where the true mean falls outside a nominal 95% interval at *some* point
while it is being watched. The fixed-N column grows with stream length — the longer you
look, the more chances to be wrong — which is the signature of the law of the iterated
logarithm and a sign the simulation is behaving.

**It wastes the budget.** A gate asking "is `MRR@10` above 0.85?" against a system scoring
0.95 is settled long before the last query, and the harness runs every one anyway. With a
BEIR-scale gold set ([ADR 0014](0014-beir-benchmark-scale.md)) that is hundreds of retrieval
round trips; with an LLM judge in the loop it is money.

The two problems have one cause and one fix.

## Decision

Add a gate policy that scores queries one at a time behind a **confidence sequence** — an
interval valid at *every* sample size simultaneously — and stops the moment the verdict is
determined.

### The construction

`eval/sequential.py` implements the hedged capital process of **Waudby-Smith & Ramdas,
"Estimating means of bounded random variables by betting" (JRSS-B, 2024)**.

To test whether the mean is `m`, bet repeatedly on each next observation at odds implied by
`m`. Wealth starts at 1. If `m` is the true mean the bets are fair, so wealth is a
non-negative martingale, and by Ville's inequality it exceeds `1/α` at *any* point with
probability at most `α`. Wealth above `1/α` is evidence against `m`, valid whenever it is
looked at — which is what makes stopping early a decision rather than a peek.

Two books are kept, one profiting when observations run above `m` and one below, hedged at
half the error budget each. Stakes are predictable — computed from the past only — which is
the condition the martingale argument rests on, and truncated so wealth stays strictly
positive and the log-space update never sees a non-positive factor.

Retrieval metrics suit this unusually well. Reciprocal rank, nDCG, recall and a judge's
groundedness score all live in `[0, 1]`, which is exactly the regime betting confidence
sequences are built for, and the construction assumes nothing else about the distribution.
Reciprocal rank is discrete, skewed and bounded — the case a normal approximation handles
worst at every sample size.

### The gate

Only the threshold itself has to be tested: the confidence sequence excludes it exactly
when the whole interval has moved to one side, so a single capital process answers the gate
question at `O(1)` per query, and which book crossed gives the direction.

Measured across the range, on a 0.85 line with a 300-query budget:

| true Recall@5 | verdict | mean stop | queries saved | wrong |
|:---:|:---:|:---:|:---:|:---:|
| 0.98 | pass | 54 | **82%** | 0.0% |
| 0.95 | pass | 89 | **70%** | 0.0% |
| 0.90 | mostly undecided | 236 | 21% | 0.1% |
| 0.85 | at the line → undecided | 293 | 2% | 1.8% |
| 0.60 | fail | 31 | **90%** | 0.0% |

Against a mock backend over 200 real HTTP round trips: a healthy system cleared in 69
requests, a broken one failed in 13. The saving is work not done, not a smaller number in
a report.

### Three decisions that follow

**Undecided fails.** The budget ran out before the evidence arrived. That is a fact about
the gold set, not a clean bill of health, and a gate treating "we could not tell" as a pass
has quietly stopped gating.

**The first metric to fail short-circuits the run.** The build is already red; confirming
the other metrics costs queries to learn nothing that changes the outcome. Those metrics are
reported as *not settled*, distinctly from *budget exhausted* — the two have different fixes.

**The gold set is read in a seeded shuffle.** The estimand is the **superpopulation** mean:
the score the system would get on the query distribution the gold set is drawn from, not the
exact average of these particular queries. That is the quantity worth gating on, and it
requires random order — a gold set arrives grouped by topic, and a sequential procedure
reading it top to bottom would decide about whatever the first section happens to contain.
The seed is recorded with the verdict.

### Scope

`sequential` evaluates only the gate mode. It answers one question as cheaply as possible,
and sweeping the other modes to fill a comparison table would spend exactly the queries it
exists to save. Use `point` or `regression` when you want the table.

## Consequences

**Peeking becomes legitimate.** The interval can be watched continuously and stopped on,
which is what everyone was already doing and now is not a mistake.

**Cost falls where there is nothing to learn.** A clearly-good system and a clearly-broken
one are both cheap; a system sitting on the threshold is expensive, which is the correct
allocation.

**A system on the line is honestly reported.** It spends its whole budget and returns
`undecided` rather than being nudged to a verdict, with type-I error inside α.

**Sequential is not the default.** On a ten-query gold set there is nothing to save. It
earns its place at benchmark scale, which is [ADR 0014](0014-beir-benchmark-scale.md).

**`anytime_experiment.py` reproduces both tables** from scratch, seeded, no stack, no
network. The numbers above are outputs, not claims.

## Alternatives considered

**Alpha spending / group sequential (O'Brien–Fleming, Pocock).** The clinical-trials
answer: valid, well understood, and it requires committing to the number and timing of
looks in advance. A CI log is inspected continuously and a gate should be able to stop at
any query, which is precisely what confidence sequences give and alpha spending does not.

**SPRT.** Optimal against a simple alternative, but needs a parametric likelihood and a
specific alternative to test against. The bound here is on a *mean* of arbitrary bounded
observations, with no distributional model at all.

**Empirical-Bernstein confidence sequences (Howard et al., 2021).** Also anytime-valid and
simpler to implement. Betting is meaningfully tighter for bounded variables at the sample
sizes in question, and tightness is the entire point — a looser sequence stops later and
saves less.

**Just evaluate everything and stop worrying.** Fine at ten queries. It does not scale to a
benchmark, and it leaves the peeking problem untouched, which is the half that produces
wrong conclusions rather than merely expensive ones.

**Sampling without replacement variants.** Tighter for a *fixed, finite* gold set, and they
answer a different question — the average over these 300 queries rather than over the query
distribution. The superpopulation mean is the one a gate should care about.
