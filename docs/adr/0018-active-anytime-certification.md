# ADR 0018 — Asking for the labels that matter: active, anytime-valid certification

Status: accepted · off by default (`recall.review.enabled`) · Extends
[ADR 0012](0012-anytime-valid-evaluation.md), [ADR 0015](0015-prediction-powered-inference.md),
[ADR 0017](0017-claim-level-conformal-factuality.md)

## Context

Every guarantee in this repository is blocked on the same input, and none of them can
supply it:

- the judge's bias correction ([ADR 0015](0015-prediction-powered-inference.md)) needs hand labels;
- the claim threshold ([ADR 0017](0017-claim-level-conformal-factuality.md)) needs hand labels;
- the drift alarms ([ADR 0016](0016-adaptive-conformal-drift.md), ADR 0017) watch the
  *judge's* view, because no true signal reaches serving.

The [labeling guide](../LABELING-GUIDE.md) ends at "budget 4–6 hours for 300 answers". The
machinery that turns labels into guarantees exists. Nothing decides which answers are worth
a person's minute, and nothing turns a trickle of labels from production into a number an
operator may read every morning.

The relevant literature:

| work | what it contributes |
|---|---|
| Zrnic & Candès, *Active Statistical Inference*, ICML 2024 ([arXiv 2403.03208](https://arxiv.org/abs/2403.03208)) | label each item with a known probability π set by model uncertainty and weight the label 1/π. Unbiased whatever the model, and PPI is the uniform-π special case |
| *Robust Sampling for Active Statistical Inference*, 2025 ([arXiv 2511.08991](https://arxiv.org/abs/2511.08991)) | uncertainty-driven π can be **worse than uniform** when the uncertainty is poor; mix with uniform and choose the mix so it is never worse |
| Xu, Karampatziakis & Mineiro, *Active, anytime-valid risk controlling prediction sets*, NeurIPS 2024 ([arXiv 2406.10490](https://arxiv.org/abs/2406.10490)) | betting-based anytime validity when labels are requested actively, with model predictions as control variates |
| Waudby-Smith, Arbour, Sinha, Kennedy & Ramdas, *Time-uniform central limit theory and asymptotic confidence sequences*, 2021–2024 ([arXiv 2103.06476](https://arxiv.org/abs/2103.06476)) | a closed-form boundary, valid at every n asymptotically, about 1.5× the one-look CLT width |
| Tibshirani, Barber, Candès & Ramdas, *Conformal Prediction Under Covariate Shift*, NeurIPS 2019 ([arXiv 1904.06019](https://arxiv.org/abs/1904.06019)) | weighted conformal quantile when calibration and test differ by a known likelihood ratio |

Each piece exists. They have not, as far as I can find, been put together for a RAG
system's own quality guarantees: in-product active labelling → an anytime-valid estimate of
the true groundedness rate → the same labels recalibrating a claim threshold that active
selection would otherwise break.

## Decision

### 1. Serving tosses a weighted coin for every judged answer

`ReviewSampler` looks up π by the judge's verdict (`recall.review.pi-{supported,partial,unsupported}`),
draws one random number, and writes a `review_item` row **whether or not** the answer is
selected. An unselected row holds the judge's prediction and π. A selected row also keeps the
question, the passages and the answer, plus the claim scores if ADR 0017 is on. π is fixed
before the coin and stored with the row. The estimator's validity rests on that single fact,
so the sampler is one lookup and one draw, and the label never influences it.

The design is three numbers because the judge emits three verdicts. That keeps serving
trivially auditable, and it means the design is changed by config, between periods, never
by data inside one.

### 2. A blind queue, a labelled prefix

`/api/admin/review` serves selected answers oldest first, with **nothing a machine said**:
no verdict, no claim score, no π. A reviewer who sees the judge's verdict agrees with it, and
the estimate would then measure the judge against itself. `/admin/review` in the frontend is
the labelling screen.

`/api/admin/review/export` returns, by default, only the **labelled prefix**: every row
older than the oldest selected answer still waiting. The queue is FIFO, so the prefix only
grows, and stopping there is a stopping time. Exporting everything and dropping the
unlabelled rows would not be one. The answers a reviewer skips are not a random sample of
those selected, and `active.py` refuses a selected row without a label rather than silently
dropping it.

### 3. The estimate: `eval/active.py`

With `f` the judge's prediction on every answer and `Y` the human label where one exists:

    Z_i = λ·f_i + (ξ_i / π_i)·(Y_i − λ·f_i)        θ̂ = mean(Z)

`E[ξ_i/π_i] = 1`, so `Z_i` is unbiased for `Y_i` whatever the judge says. λ is the PPI++
power-tuning weight. Three intervals are reported:

| interval | valid when | read it |
|---|---|---|
| fixed-sample (CLT) | one look at a pre-planned n | for a report written once |
| **exact anytime** (betting, per-step bet caps from the range Z can take) | every n, no approximation | always safe; wide while π is small |
| **asymptotic anytime** (Gaussian-mixture boundary) | every n, asymptotically | from the 60th label on — the one to put on a dashboard |

### 4. The design is learned, not assumed

The textbook rule, π ∝ √(f(1−f)), says a SUPPORTED or UNSUPPORTED verdict is never wrong.
Such answers are then sampled at the floor rate, and each one that turns out wrong carries
weight 1/floor. `ResidualModel` instead learns each verdict's error rate from the previous
period's labels, weighted back to traffic, and shrinks it toward 0.25 with no data. With no
labels at all it returns uniform, so the cold start is plain PPI. `choose_mix` then picks
the uniform share on those labels, uniform included, following arXiv 2511.08991.
`active.py --budget 0.05` prints next period's three probabilities as YAML.

### 5. Claim recalibration from the same labels

If the reviewer also marks each claim, the same rows recalibrate ADR 0017's threshold. They
must be reweighted. An actively chosen sample over-represents what the judge found hard, so
the plain conformal quantile answers a question about a different population.
`calibrate_strict_weighted` weights each answer 1/π, which is the traffic-to-sample
likelihood ratio, and gives the test point the bound 1/floor. That makes one threshold valid
for every answer, at a small cost in conservatism.

## Results

`make eval-active` reproduces everything, seeded, with no stack. Full tables are in
[active-results.md](../active-results.md). The judges are simulations spanning the ways a
three-verdict judge goes wrong, not measurements of this system's judge.

**Which answers to label.** 4000 answers per period, 5% budget, 300 runs:

| judge | uniform (= PPI) | textbook active | learned active |
|---|:---:|:---:|:---:|
| calibrated | 93.3% · 0.091 | 94.0% · 0.130 (−102% labels) | 96.7% · 0.086 (**+10%**) |
| flattering | 94.3% · 0.108 | **89.0%** · 0.186 (−198%) | 95.0% · 0.104 (**+7%**) |
| confidently wrong | 94.7% · 0.140 | 91.0% · 0.206 (−116%) | 96.7% · 0.131 (**+12%**) |

*(coverage of the 95% interval · mean width; in brackets, labels saved relative to uniform at
equal width.)*

The honest headline is the middle column. The rule from the paper is the one anyone would
implement first, and against a three-verdict judge it needs **twice to three times the
labels** of plain random sampling. Its coverage also slips to 89% against a flattering judge,
because a few 1/floor-weighted labels make the CLT a poor approximation. The learned design
fixes both, but the gain over uniform is 7–12%, not the 80% the paper reports. A judge with
three output values carries little information about *where* it errs. A continuous signal,
such as the claim scores, is the obvious next stratum, and the code takes any key.

**Reading it every day.** Each run is read after every one of 4000 answers:

| judge | exact: ever wrong · width | asymptotic: ever wrong · width | one-look width |
|---|:---:|:---:|:---:|
| calibrated | 0.0% · 0.344 | 2.0% · 0.144 | 0.088 |
| flattering | 0.0% · 0.338 | 6.0% · 0.172 | 0.105 |
| confidently wrong | 0.0% · 0.434 | 0.7% · 0.311 | 0.131 |

The exact sequence never failed and is 3–4× too wide to steer by, because every bet has to
survive a label weighted 1/π. The asymptotic sequence is the usable one, at about 1.6× the
one-look width. Before 60 labels it is not valid: read from the first answer, it excluded
the truth in **30–70%** of runs against a 5% promise, because the variance estimate has not
yet seen the heavy terms. So it reports [0, 1] until then. The 6.0% for the flattering judge
is from 150 runs (standard error about 1.9 points), consistent with 5%. An asymptotic
guarantee is what it says it is.

**Recalibrating the claim threshold** (promise: 90% of answers fully supported):

| judge | naive quantile on actively chosen answers | weighted (1/π) |
|---|:---:|:---:|
| calibrated | 92.7% | 91.5% |
| flattering | 94.0% | 91.5% |
| confidently wrong | **60.2%** | 91.7% |

This is the finding to remember. When the judge is unsure about the easy answers and certain
about the ones it gets wrong, active selection hands the calibration step a sample that is
too clean. The naive threshold then keeps every claim, and the 90% promise delivers 60%,
with nothing in the calibration output to show it. The reweighted threshold holds the
promise under every judge.

## Consequences

- The labelling step, the bottleneck of ADR 0015 and 0017, now has a product surface, a
  budget knob and a stopping rule. Labels arrive as a side effect of operating the system
  instead of as a 4–6 hour project.
- `FactualityMonitor` and `CoverageMonitor` still watch the judge's view. The true rate now
  has its own anytime-valid estimate, but it lives in the export path, not in a gauge, because
  the statistics live in Python with their tests. Porting `ActiveConfidenceSequence` to Java
  behind golden vectors, as `CoverageMonitor` was, is the step to put it on the dashboard.
- One DB row per judged answer. The row is a handful of numbers unless selected.
- The estimand is the true rate **over judged answers**. Cache hits replay an answer judged
  earlier and are not re-sampled, and abstentions are not judged.
- What it cannot fix: validity is relative to the labels.
  [LABELING-GUIDE.md](../LABELING-GUIDE.md) and its two-person agreement check remain the
  part that decides whether the labels, and so the estimate, are right.

## Alternatives considered

- **Label a uniform random sample (PPI as in ADR 0015).** Valid, and the cold start of this
  design. Without the ledger it cannot be done continuously from production.
- **Uncertainty sampling without weights** (classic active learning). Selects well, then
  estimates the wrong population. Section 5 shows a 90% promise delivering 60%.
- **The textbook √(f(1−f)) design.** Implemented, measured, kept as a negative result and a
  test, so that nobody "simplifies" back to it.
- **Only the exact betting sequence.** Never wrong and too wide to act on. Shipped as the
  fallback, not the headline.
