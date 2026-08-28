# ADR 0016 — The certificate expires, and nothing tells you

Status: accepted · Extends [ADR 0013](0013-conformal-risk-control.md)

## Context

[ADR 0013](0013-conformal-risk-control.md) calibrates a conformal threshold once and writes
it into `application.yml`:

```yaml
recall.rag.conformal.threshold: 0.5286119812181718
```

Eighteen significant figures, and every one of them conditional on an assumption stated
nowhere near the number: **exchangeability**. The queries the threshold was calibrated on
and the queries it will serve have to be draws from the same distribution.

A running system stops satisfying that almost immediately. Documents are ingested, so the
corpus the reranker scores against is not the corpus it was calibrated on. Query mix moves
with whatever the users are doing this month. Someone swaps the embedding model. A
certificate calibrated in January is not valid in June — and, matching every other failure
this repository has chased, **nothing notices**. The threshold is still in the config, the
dashboards still render, `/metrics` still reports, and the 90% coverage it promises has
quietly stopped being true.

That is the same shape as the founding audit: a number that is precise, load-bearing, and
no longer connected to reality.

## Decision

Adopt **adaptive conformal inference** (Gibbs & Candès, *Adaptive Conformal Inference Under
Distribution Shift*, NeurIPS 2021), in `eval/adaptive.py`. The level becomes a control
variable driven by realised miscoverage:

```
alpha_{t+1} = alpha_t + gamma * (alpha - err_t)
```

`err_t` is 1 when the set at step `t` missed. A miss **lowers** `alpha_t`, and a lower level
is a **wider** set — the quantile is taken at rank `ceil((n+1)(1-alpha))`, so less alpha
keeps more of the ranking. Miss too often and the set grows; miss too rarely and it shrinks.

This is not a heuristic wrapped in notation. Summing the update telescopes exactly:

```
(1/T) sum(err_t) - alpha = (alpha_1 - alpha_{T+1}) / (T * gamma)
```

and `alpha_t` cannot leave `[-gamma, 1 + gamma]`, because an empty set always misses and a
full set never does, so the recursion self-corrects at both ends. Realised long-run
miscoverage therefore converges to `alpha` at rate `O(1/T)` with **no distributional
assumption at all** — not exchangeability, not stationarity, not even that the shift is
random rather than chosen by an adversary who has read this file. What it gives up is the
finite-sample guarantee at any single step; what it buys is a guarantee that survives the
corpus changing underneath it.

`gamma` is the usual awkward constant — too small and it cannot keep up, too large and it
thrashes on noise — so `DtACI` (Gibbs & Candès, JMLR 2024) runs several candidates as
experts and aggregates them by their own realised pinball loss. That is possible here
because every expert's counterfactual outcome is computable after the fact: coverage at
level `a` is just whether the observed score exceeded the quantile at `a`.

## What the measurement changed

The experiment (`eval/adaptive_experiment.py`, 200 independent streams of 3,000 queries per
scenario, change at query 1,000) was written to confirm the story above. It corrected it
instead, and the corrected version is in the module docstring and the tests.

| scenario | fixed | window | adaptive | level offset | compensating |
|---|---|---|---|---|---|
| location shift (+0.30) | 0.459 | 0.876 | 0.900 | 0.027 | 0% |
| score saturation | 0.306 | 0.939 | 0.903 | 0.514 | 100% |

Coverage promised: **90%**. `fixed` is the ADR 0013 arrangement; `window` recalibrates on a
rolling window of recent scores at a level that never moves; `adaptive` adds the controller.

Three things worth reading off it.

**The frozen threshold fails, badly.** It promises 90% and delivers 45.9% and 30.6%; the
worst single stream reaches 28.4%. This is the gap ADR 0013 has and did not know it had.

**A rolling window does most of the work against a location shift — the controller is not
what saves that case.** Refill the calibration set with recent scores and the quantile moves
with the data, so the level has nothing left to correct. The tests check this as an identity
rather than describing it: over 40 seeds each at shifts of 0.00, 0.20 and 0.30, the widest
excursion of the terminal level from target is the *same* 0.100 in all three. The shift
never reaches the controller. An earlier draft of this ADR claimed the controller was
detecting and correcting drift here; it was not, and the number that looked like evidence
turned out to be an artefact of the test generator clamping scores at 1.0.

**The controller earns its place on the residual, and on the case the window cannot track.**
Even where the window succeeds it is systematically off — 87.6%, undercovering by 2.4
points — and the controller closes that to 90.0%. Under saturation the window errs the other
way, *over*covering at 93.9%, which is not a safety win but wasted prompt tokens on sets
larger than the guarantee requires; the controller pulls it to 90.3%. Recovery after the
change is under 100 queries in 100% of streams, and the deterministic bound held in 100%.

## `compensating`, and why it is not called `drifting`

The obvious name for "the level has moved a long way from target" is a drift detector. That
name is wrong, and the measurement above is why: a pure location shift leaves it **False**,
because the window absorbs the shift and no offset is needed. What the signal actually
reports is that the window quantile is systematically wrong and only a displaced level is
holding coverage together — a change of shape, a mass of tied scores, an adversarial stream.

That is the more useful of the two signals, and it is the one a frozen threshold can never
produce. Under exchangeability the widest stationary excursion measured over 2,000 steps at
`gamma = 0.05` was 2.6 gamma, so the 4 gamma trigger sits above the noise with room to
spare: 0/60 false positives on stationary data, 0/60 on an absorbed location shift, 60/60 on
saturation.

## Consequences

**A serving threshold can now be wrong out loud.** `report()` carries the standing offset,
whether the bound held, and `compensating`. A threshold that has stopped meaning what it
says becomes a signal instead of a silence.

**The saturating reranker is now detectable.** Once most candidates share the top score, no
threshold separates them and every downstream guarantee degrades quietly. This is a real
pathology, not a synthetic one, and nothing in the system could previously see it.

**The guarantee changes shape, and the docs must say so.** ADR 0013 offers finite-sample
coverage at every step under exchangeability. This offers long-run coverage under nothing at
all. Neither dominates, and pretending the second is a strict upgrade would be exactly the
kind of claim this repository exists to catch.

**Cost is one float per query.** No model, no retraining, no dependency; `eval/adaptive.py`
is standard library only, like the rest of `eval/`.

## Alternatives considered

**Recalibrate nightly on recent traffic.** Most of the benefit for much less machinery — and
the measurement above is what makes that a fair statement rather than a guess, since the
window alone reaches 87.6% where the frozen threshold reaches 45.9%. Rejected as the whole
answer because it is silently off target even when it works, has no guarantee when the shift
is faster than the schedule, and cannot distinguish "tracking fine" from "only a displaced
level is holding this together". It remains a sound fallback for a deployment unwilling to
run a control loop, and it is now a measured fallback rather than an assumed one.

**Drift detection on the score distribution** (KS test, MMD, PSI). Detects change but does
not act on it, needs a reference window and a threshold of its own, and fires on changes
that do not affect coverage at all — as the location-shift column demonstrates, most of them
do not.

**Retrain or recalibrate on a schedule, with the threshold pinned between runs.** Where ADR
0013 already is. The failure mode is the one at the top of this file: between runs it is
silently wrong, and the schedule is chosen by guess rather than by measurement.
