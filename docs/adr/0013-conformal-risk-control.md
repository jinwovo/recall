# ADR 0013 — The constants that decide cost and hallucination become certificates

Status: accepted · Supersedes the threshold in [ADR 0009](0009-sufficient-context-bbq.md) ·
Amended by [ADR 0016](0016-adaptive-conformal-drift.md), which measures what happens to
this certificate once the corpus stops standing still

## Context

Two numbers in the serving path decide what this system costs and how often it can make
things up:

```yaml
recall.retrieval.top-k: 8                              # passages that reach the LLM
recall.rag.sufficiency.confidence-threshold: 0.35      # when to answer at all
```

Both were chosen by hand. They are the least rigorous artefacts in a repository whose whole
argument is that quality claims should be measured, and they control more of the system's
behaviour than anything that *is* measured.

A fixed top-K is wrong in both directions at once, and the two errors do not cancel:

- On a query where the reranker found one obviously-right passage, seven more are prompt
  tokens spent on noise — cost, prefill latency, and a wider surface for the model to
  wander into.
- On an ambiguous query where the scores are nearly flat, the same K truncates the passage
  that held the answer. Nothing downstream reports it. The generation proceeds from context
  that cannot support it, which is the condition under which grounded RAG hallucinates.

The abstention threshold has the same shape of problem. `0.35` implies a claim about how
often the system answers when it should not, and no such claim was ever made or checked.

## Decision

Both become thresholds calibrated on held-out queries, carrying **finite-sample,
distribution-free guarantees**. No assumption about the score distribution, no asymptotics,
no requirement that the reranker be well calibrated — only that calibration and deployment
queries are exchangeable. That assumption is load-bearing and has a shelf life —
[ADR 0016](0016-adaptive-conformal-drift.md) measures the frozen threshold below
delivering 45.9% coverage against the 90% it promises, once the score distribution
moves — so it is stated here rather than left implicit.

### Coverage: adaptive context sizing

Split conformal prediction with adaptive set sizes (Vovk et al.; Papadopoulos et al. 2002;
Romano, Sesia & Candès, NeurIPS 2020). Reranker scores become a distribution over
candidates; the context is the shortest prefix whose *exclusive* cumulative mass stays at
or below a calibrated quantile. Then

```
P(the passages sent to the LLM contain a relevant document) >= 1 - alpha
```

for the next query, exactly, at any sample size. The `(n+1)` correction on the quantile rank
is what makes it exact rather than asymptotic; without it the procedure under-covers, and
the gap is invisible in every in-sample number.

The budget moves to where it is needed. Measured on an 800-query calibration against a mock
cross-encoder: **4.8 passages mean against a conformal fixed K of 8 carrying the identical
promise — 40% fewer prompt tokens** — with held-out coverage inside its interval. Broken
down: queries whose answer ranked first get about two passages, queries whose answer ranked
fourth or lower get nine.

A degenerate reranker does not break the guarantee, it pays for it in context length: with
scores shuffled free of the labels, coverage still holds and mean set size grows to fill the
candidate list. That is the distribution-free property doing its job — the cost of a bad
model shows up as tokens, never as a silent miss.

### Risk: calibrated abstention

Risk-controlling prediction sets (Bates, Angelopoulos, Lei, Malik & Jordan, JACM 2021) with
fixed-sequence testing over a threshold grid, bounding the probability of answering from
context with no relevant document in it:

```
P( R(threshold) <= risk-alpha ) >= 1 - delta
```

The outer probability is over the calibration draw. In words: with 95% confidence, the
deployed abstention policy answers-when-it-should-not at most 5% of the time. That is a
statement an operator can hold the system to.

The p-value is the minimum of the Hoeffding and Bentkus bounds — sharp in different regimes,
and a loose bound spends the error budget on nothing and leaves a needlessly conservative
threshold in production. Walking a **fixed sequence** from most conservative to least
controls the family-wise error rate with no multiplicity correction at all, so testing 46
candidates costs exactly as much budget as testing one.

### One run, three splits

`eval/calibrate.py` produces both certificates from a single pass over the gold set and no
extra labelling: the retrieval that scores `Recall@5` already records where the relevant
document landed and how confident the reranker was, which is everything both calibrations
need.

| split | what it decides |
|---|---|
| tuning | the softmax temperature |
| calibration | the conformal quantile and the risk threshold |
| held-out | scores both, having informed neither |

The temperature split is not ceremony. The right temperature depends entirely on the scale
a reranker head emits — raw cross-encoder logits want `T ≈ 1`, scores squashed into `[0, 1]`
want `T ≈ 0.1` — and at the wrong one the softmax is nearly uniform, every set is the full
candidate list, and **the guarantee still holds while the entire benefit quietly
disappears**. Choosing it on the same data the quantile is fitted on would break
exchangeability, and the resulting under-coverage would not show up in any in-sample number.

### Serving must agree with calibration

`ConformalSetSizer` (Java) implements the serving side, and its agreement with the Python
calibrator *is* the certificate. A guarantee is a property of one procedure: if serving
computes a different K than calibration assumed, `alpha` means nothing and the system
carries a certificate for a rule it does not run.

`ConformalSetSizerTest` pins that agreement against 24 golden vectors generated by the
Python implementation, covering the awkward shapes — one dominant passage, an exact tie,
all-negative logits, a single candidate — each chosen at least `1e-9` from the inclusion
boundary so a last-ulp difference between the two `exp` implementations cannot flip an
answer.

### Off until certified

Ships disabled with `threshold: -1`. A threshold outside `[0, 1]` is not a conservative
setting, it is an unconfigured one — the quantity being compared is a probability — so the
component refuses it, logs why, and the pipeline keeps its fixed top-K until
`make calibrate` produces a real certificate.

### Honest failure

A certificate that abstains on everything is valid, useless, and labelled **degenerate**;
shipping it would be worse than shipping the constant it replaced. A threshold walk that
stalls names the calibration size that would settle it. On 400 gold queries the risk bound
cannot be proven and the tool asks for about 1,100; on 2,000 it certifies a threshold
answering 46% of queries at a proven ≤ 5% risk, with held-out risk 3.6%.

## Consequences

**Prompt cost falls and the promise gets stronger at the same time**, which is not a
trade-off anyone had to make — it was available the whole time and a fixed K was leaving it
on the table.

**`retrieval.top-k` becomes a fallback** rather than the policy: the value used when no
certificate exists.

**Recalibration is required** when the corpus, the reranker, or `alpha` changes. The
certificate records all three, and the report says so.

**Calibration needs queries.** At `alpha = 0.02` the minimum calibration set is 49 queries
before anything can be certified at all, and the risk bound needs far more than that to be
worth having — another reason [ADR 0014](0014-beir-benchmark-scale.md) exists.

**The guarantee is marginal, not conditional.** Coverage holds in expectation over
calibration draws, so a single split lands in a Beta around `1 - alpha` rather than exactly
on it. The report prints the held-out coverage *with its exact binomial interval* precisely
so that a valid calibration is not mistaken for a broken one.

## Alternatives considered

**Tune the constants on the eval set.** What was already happening, and what
[ADR 0011](0011-statistical-inference-eval.md) showed produces noise. It also yields no
guarantee, only a number that looked good once.

**Calibrate the reranker's probabilities (Platt scaling, isotonic).** Useful, and solving a
different problem: it makes scores better calibrated on average, which is not the same as a
per-query guarantee, and it needs the calibration to transfer to a new corpus. Conformal
needs the model to be neither good nor calibrated.

**A fixed but conformally-chosen K.** Implemented and kept as the honest baseline in every
comparison, because it carries the same coverage promise. It is simply worse: it cannot move
the budget between queries, so it spends 8 passages where 2 would do and still truncates the
hard ones at 8.

**Bound hallucination directly with an LLM judge in the loop.** The right ultimate target
and much more expensive to calibrate — every calibration query costs a judge call. The
proxy bounded here (answering when the context holds no relevant document) is the *condition*
that makes ungrounded generation possible, is free to label from the same retrieval run, and
composes with the post-hoc judge of [ADR 0004](0004-groundedness-guardrail.md) rather than
replacing it.

**Skip the temperature split and pick T by eye.** Half the code, and it silently costs the
entire benefit when the score scale changes. The failure mode is a passing report with no
improvement in it, which is the worst kind.
