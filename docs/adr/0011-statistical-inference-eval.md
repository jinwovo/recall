# ADR 0011 — Retrieval scores are estimates, and are reported as such

Status: accepted · Supersedes the reporting half of [ADR 0007](0007-eval-ci-regression-gate.md)

## Context

[ADR 0007](0007-eval-ci-regression-gate.md) made retrieval quality a build failure rather
than an opinion, and that was the right move. But it gated on point estimates, and the
README quoted point estimates, and neither said anything about how much those estimates
could be trusted.

Auditing the shipped numbers made the problem concrete. The gold set has ten queries with
exactly one relevant document each, so `Recall@5` is a per-query coin flip and the reported
`1.00` means ten successes out of ten. Three facts follow immediately, none of which were
anywhere in the repository:

1. **`Recall@5 = 1.00` is `[0.69, 1.00]`.** The exact binomial interval at 10/10 has a
   lower bound of `(α/2)^(1/n) = 0.025^0.1 = 0.6915`. The headline number was 31 points
   wide and presented as a fact.
2. **The headline lift cannot be demonstrated.** Hybrid beats BM25 on `Recall@5` by +0.30,
   which on ten queries means three queries flipped. A paired randomization test on three
   differing pairs has a smallest attainable two-sided p-value of `2/2³ = 0.25`. Not "the
   result was not significant" — *no* outcome of that comparison could have been.
3. **The self-tuning loop was measuring nothing.** [ADR 0010](0010-tool-round-ingest-mcp-action-selftune.md)
   opens a PR when a grid point beats the baseline `MRR@10` by 0.01. Resolving 0.01 at the
   per-query spread this corpus produces needs several thousand queries. On ten it selects
   the largest random deviation among nine candidates and writes it into `application.yml`.

The system was engineered. The measurement was not.

## Decision

Every reported number carries the inference that makes it a claim, and the harness states
what the gold set can resolve rather than leaving it to be discovered.

### Intervals chosen from the metric's support

`eval/stats.py` reports a 95% interval on every metric, and picks the method from what the
per-query values actually are:

- **Exact binomial (Clopper–Pearson)** when the per-query metric is 0/1 — `Recall@k` with a
  single relevant document. Conservative by construction, and defined at the `0/n` and
  `n/n` boundaries where everything else fails.
- **BCa bootstrap** otherwise, with jackknife acceleration and a percentile fallback when
  the bias correction is undefined.

The choice matters at exactly the point where it is most tempting to skip it: on a perfect
score the bootstrap resamples a constant and returns `[1.00, 1.00]`, which reads as
certainty and is not.

### Paired randomization tests, Holm-corrected

Mode comparisons use the paired randomization test standard in IR
(Smucker, Allan & Carterette, CIKM 2007): the null is that the two systems are
interchangeable per query, so every sign assignment of the per-query differences is equally
likely. Exact by enumerating all `2^k` assignments up to 16 non-tied pairs, seeded Monte
Carlo above that.

No normality assumption, which is not pedantry — reciprocal rank takes values in
`{1, ½, ⅓, …, 0}` and is heavily skewed, and a t-test on ten of them is not measuring what
it claims to. p-values are **Holm-corrected** across the modes compared within each metric:
a five-mode sweep against one baseline is five chances at a false win, and an uncorrected
0.05 is about 0.23.

### `effective_n` and the p-value floor

Queries scoring identically under both systems carry no signal, so the sample size that
counts is the number that *differ*. The floor `2/2^k` is reported alongside every
comparison, and a comparison whose floor already exceeds 0.05 is labelled **unresolvable**
rather than "not significant" — the first is a fact about the gold set, the second implies
something was measured.

### Design analysis, before anything runs

`eval/power_report.py` answers three questions from the labels alone, with no stack and no
network: the p-value floor at each `k`, the interval width this `n` buys (including for a
perfect score), and the queries a target improvement would need at 80% power. Running it on
the shipped gold set is how the three findings above were produced.

### Gate policies

`--gate-policy` gains `ci-lower` (the 95% lower bound must clear the threshold) and
`regression` (a paired test against a recorded run, so a real two-query drop is caught while
the mean still clears the line). `point` stays the default: on ten queries the interval is
wider than the whole safety margin, so adopting `ci-lower` is a decision to grow the gold
set, not to loosen the gate.

### The tuner has to prove itself

`eval/tune.py` now searches a stratified dev split, and a proposal must survive four guards,
all reported whether they pass or fail: an effect-size floor, a Holm-corrected paired test
across the whole grid, and a held-out split that the search never touched and that must
still show the improvement. The dev-minus-held-out gap is printed as the overfitting the
search introduced. When the guards fail for want of data, the report names the query count
that would settle it instead of suggesting a smaller epsilon.

On the shipped ten-query set this means the tuner will essentially never fire. That is the
correct behaviour.

## Consequences

**The headline numbers got weaker and the repository got stronger.** `Recall@5 = 1.00
[0.69, 1.00]` is a less impressive sentence and a more defensible one, and the +0.30 lift
is now labelled unresolvable at this sample size. A reader who checks will find the claims
survive checking.

**Standard library only.** The harness ships inside the `rag-eval-gate` composite action
and external repositories run it with no install step, so the regularized incomplete beta
is implemented here rather than imported. `eval/test_stats.py` cross-checks it against
SciPy when SciPy happens to be present, and skips when it is not.

**Deterministic.** Every resampling routine is seeded. A gate that flaps because the
bootstrap drew differently is worse than no gate.

**The eval harness is tested like production code**, because it is: if it computes nDCG
wrong, every number downstream is wrong and nothing else in the build will say so. Tests
run against closed forms, an independent brute-force permutation reference, and empirical
BCa coverage against a skewed population.

**The gold set is the bottleneck, and now says so.** Every tool here points at the same
conclusion — ten queries cannot resolve what anyone wants to act on. That is what
[ADR 0014](0014-beir-benchmark-scale.md) is for.

## Alternatives considered

**Report point estimates and add a disclaimer.** A sentence saying "small sample" does not
tell a reader that a perfect score bottoms out at 0.69, and it does not stop a tuning loop
from acting on noise.

**Bootstrap everything.** Simpler, one code path, and wrong exactly where it matters: at
`n/n` the resample is constant and the interval collapses to a point.

**t-tests instead of randomization tests.** Faster and standard elsewhere. Reciprocal rank
is discrete, bounded and skewed, and at n = 10 the normal approximation is not defensible.

**Bonferroni instead of Holm.** Simpler and uniformly less powerful, for no gain.

**Drop the small gold set and go straight to a benchmark.** The right end state, and the
subject of [ADR 0014](0014-beir-benchmark-scale.md) — but the inference layer is what makes
a larger benchmark worth having, and it is what proved the smaller one was not.
