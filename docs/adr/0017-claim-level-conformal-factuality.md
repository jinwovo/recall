# ADR 0017 — The guarantee reaches the answer: claim-level conformal factuality

Status: accepted · serving in observe mode until a labelled claim set exists · Extends
[ADR 0013](0013-conformal-risk-control.md), [ADR 0015](0015-prediction-powered-inference.md)

## Context

Every certificate in this repository stops one step short of the answer.
[ADR 0013](0013-conformal-risk-control.md) certifies that the passages sent to the LLM
contain a relevant one, and when to abstain. [ADR 0015](0015-prediction-powered-inference.md)
puts a valid interval on the judge's verdicts. Neither says anything about *the sentences
the reader sees*.

The groundedness judge grades an answer as one unit. An answer with five supported claims
and one invented one gets one verdict: pass it, and the invented claim ships with the
others' credibility; fail it, and five correct claims are thrown away to remove one. The
reader is never told which sentence was the problem.

The literature moved here between 2024 and 2026:

| work | what it contributes |
|---|---|
| Mohri & Hashimoto, *Language Models with Conformal Factuality Guarantees*, ICML 2024 ([arXiv 2402.10978](https://arxiv.org/abs/2402.10978)) | split the output into claims, drop the low-scoring ones at a conformally calibrated threshold → P(every kept claim is true) ≥ 1−α |
| Feng et al., *Conformal-RAG*, SIGIR 2025 ([arXiv 2506.20978](https://arxiv.org/abs/2506.20978), [code](https://github.com/n4feng/ResponseQualityAssessment)) | score claims *against the retrieved passages*; group-conditional guarantees across sub-domains; up to 60% more claims kept at the same guarantee |
| Kang et al., *C-RAG*, ICML 2024 ([arXiv 2402.03181](https://arxiv.org/abs/2402.03181), [code](https://github.com/kangmintong/c-rag)) | certified upper bound on RAG generation risk |
| Chen et al., *Is Conformal Factuality for RAG-based LLMs Robust?*, 2026 ([arXiv 2603.16817](https://arxiv.org/abs/2603.16817)) | entailment verifiers match LLM confidence scorers at >100× fewer FLOPs; high factuality levels give **vacuous** outputs; the guarantee **does not survive distractors or distribution shift** |

The 2026 paper is the reason this ADR exists in the form it does. It is an audit of the
technique, and its findings are the same three this repository already reports for its own
certificates: a guarantee can be met by producing nothing, it is conditional on
exchangeability, and a scorer is a measuring instrument with its own error.

## Decision

`eval/factuality.py` implements three calibrations over answers decomposed into scored,
labelled claims, sharing one filter (`score > threshold`):

1. **`all-supported`** (Mohri & Hashimoto). Nonconformity of an answer = the highest score
   any unsupported claim in it got. Its `(n+1)`-corrected conformal quantile is the
   threshold. Exact: P(all kept claims supported) ∈ [1−α, 1−α+1/(n+1)].
2. **`expected-share`** (conformal risk control, Angelopoulos et al., ICLR 2024). Loss =
   share of the answer's claims that are false *and* kept — normalised by total claims so
   it is monotone in the threshold. Bound: E[loss] ≤ α. A weaker promise, offered because
   the strict one is what empties answers, and labelled as weaker.
3. **Per group** (Mondrian; Conformal-RAG's group-conditional form). One certificate per
   domain. A group too small to certify is refused, not pooled with the others.

And two refusals, following ADR 0013's degenerate-certificate rule:

- **uncertified** when n < ⌈1/α⌉−1 — the threshold is +∞ and every claim is withheld;
- **vacuous** when the threshold keeps fewer than 20% of supported claims — certified, and
  useless, and reported as such rather than shipped.

### Scorer

The intended serving scorer is the bge cross-encoder that already reranks passages:
`score(claim) = max over sent passages of rerank(claim, passage)`. That is the
"lightweight entailment verifier" the 2026 study finds as good as an LLM scorer at a
hundredth of the cost, and it is already deployed, warm, and paid for.

## Results

`make eval-factuality` — seeded simulation, no stack. α = 0.10, calibrate on 300 answers,
deploy on 1,000, mean of 300 cycles. Full output: [`docs/factuality-results.md`](../factuality-results.md).

| filter | answers fully supported | false-and-kept share | supported claims kept | answers emptied |
|---|:---:|:---:|:---:|:---:|
| no filter | 38.4% | 18.7% | 100.0% | 0.0% |
| `all-supported` | **90.0%** | 2.1% | 88.3% | 1.5% |
| `expected-share` | 61.2% | **9.8%** | 99.3% | 0.2% |

| calibration | easy: fully supported | hard: fully supported | hard: claims kept |
|---|:---:|:---:|:---:|
| marginal | 97.9% | **82.4%** | 85.9% |
| per group | 90.3% | **90.2%** | 77.6% |

A marginal 90% that is 98% on easy questions and 82% on hard ones is the failure
group-conditional calibration exists to fix, and the cost is visible in the same row: the
hard group keeps fewer claims, because holding it to 90% on its own takes a stricter cut.

**Shift.** Calibrated on clean retrieval and deployed against distractors that make false
claims look entailed: 89.9% → **52.1%** fully supported, against a promised 90%. This is the
2026 paper's finding reproduced, and it is kept as a test (`test_distractors_break_the_promise`)
so the limitation stays measured rather than forgotten. The response is ADR 0016's: the
certificate expires, and the coverage monitor is the thing that says so.

## What these numbers are not

They are not a measurement of this system. The scorer is simulated (Beta(5,2) for supported
claims, Beta(2,5) for false), and the real cross-encoder's separation on decomposed claims
is unknown until there are answers with human claim labels. What the simulation does
establish is that the *procedure* keeps its promise, at the boundaries where it is easiest
to get wrong: `test_the_finite_sample_corrections_are_what_hold_the_line_at_small_n` fails
for a version without the `(n+1)` quantile correction (85% coverage at n = 19) and for risk
control without its `+B` term (10.2% risk against 10%). At n = 200 both broken versions
pass every other test, which is why that one exists.

## Serving

`ClaimVerifier` runs in the post-answer tail beside the judge (`Mono.zip`, so the tail waits
for the slower of the two, not their sum):

- **Decomposition** is sentence splitting (`ClaimSplitter`), not an LLM call. Deterministic,
  free, and every claim is an exact span of the text the reader already has, so the UI can
  strike it in place. A compound sentence is scored as one claim, which can only make the
  filter stricter. Citation markers stay in the span and are removed from the scored text.
- **Scoring** is `max` over the sent passages of the cross-encoder score — one `/rerank`
  call per claim, four in flight, capped at `max-claims`. Claims past the cap are unscored,
  kept, and **void the certificate** for that answer (`certified: false` on the event).
- **Filtering** is `score > threshold`, strictly, matching the calibration. Withheld claims
  are struck through rather than removed, with the score and threshold on hover.
- **Fail-open** on timeout or sidecar error, counted as `recall.rag.claims.skipped`.
- **Cache**: an answer with a withheld claim is not cached — the cache stores the text, and
  replaying it would replay the claim the filter removed.
- **Observe mode** (`threshold: -1`): scored and reported, nothing withheld. This is how the
  calibration set is collected, in three commands:
  1. `make collect-claims` runs the questions through `/api/ask` and writes each answer's
     claims — text, score, `"supported": null` — to `eval/claims-to-label.jsonl`. Cache hits,
     abstentions, answers with unscored claims and answers served under an *active* filter
     are skipped and counted: none of them is the population the threshold will filter.
  2. A person sets every `null` to `true` or `false`, following the
     [labeling guide](../LABELING-GUIDE.md). Each row carries the passages the LLM was shown,
     numbered as the answer cites them, because those are the only thing a label may judge.
  3. `make certify-claims` prints the certificate and the YAML to paste, and exits non-zero
     when it is uncertified or vacuous. A claim still `null` is refused, not read as
     `false` — `bool(None)` would have silently pushed the threshold up.

### The certificate expires too

`FactualityMonitor` gives the claim certificate the alarm ADR 0016 gave the coverage one,
on the same anytime-valid machinery — now one class, `AnytimeRateMonitor`, shared by both
monitors, with the Python golden vectors still pinning it. Two choices are specific to
claims:

- **What is judged is what the reader was left with.** Judging the full answer would count
  the claims the filter correctly removed as failures of the filter. With nothing withheld
  the ordinary verdict is reused at no cost; otherwise the remaining text is judged once
  more, detached from the response, since nobody is waiting on a number only the alarm
  reads. With every claim withheld nothing is left to be wrong — the calibration counts
  that as factual, and so does the monitor.
- **`PARTIAL` is a miss.** The coverage monitor lets it pass; this promise is about *every*
  remaining claim, and "partially supported" says one of them is not.

Only certified answers are observed — an answer with unscored claims was never promised
anything. In the distractor regime from the simulation (~48% misses against a 10%
promise) the alarm latches within 60 answers; at 8% it stays quiet over 2,000.

Checked in a browser against a stub stream: the withheld sentence renders struck through at
exactly its offsets, citations inside and around it stay clickable, and the badge reads
"1 of 3 claims withheld".

## Consequences

- The answer gains a promise about its own sentences, not only about its context.
- No threshold ships, because the labelled set of decomposed answers does not exist yet.
  The judge-label format of ADR 0015 extends naturally — one row per claim instead of per
  answer — and PPI is how a small human-labelled set and a large judge-labelled one combine.
- One more threshold to keep exchangeable, and now one more alarm watching it. Its signal is
  the judge's, so the ADR 0015 caveat carries over: a drift in the judge is
  indistinguishable here from a drift in the claims.
