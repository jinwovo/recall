"""Statistical inference for retrieval metrics (docs/adr/0011).

A retrieval score is an *estimate* from a finite query sample, not a measurement of the
system. This module supplies the inference that turns `MRR@10 = 0.95` into a claim you can
defend: an interval around it, a p-value against a baseline, a correction for having asked
several questions at once, and — most usefully — an honest statement of what a gold set of
size n can and cannot resolve.

What lives here:

* `bootstrap_ci`      — BCa bootstrap interval for any per-query mean (MRR, nDCG, ...).
* `clopper_pearson`   — exact binomial interval, for metrics that are per-query 0/1
                        (Recall@k with a single relevant document). The bootstrap is
                        degenerate at 0/n and n/n; the exact interval is not.
* `paired_permutation_test` — the IR-standard randomization test for system A vs system B
                        (Smucker, Allan & Carterette, CIKM 2007). Exact by enumeration for
                        small samples, Monte Carlo above that.
* `holm`              — Holm-Bonferroni step-down correction: sweeping five modes against a
                        baseline is five tests, and an uncorrected 0.05 is not 0.05.
* `design_analysis`   — the part that matters most for a small gold set: how large an effect
                        this n can detect at all, and the smallest p-value it can produce.

Design notes:

* **Standard library only.** The eval harness ships inside a composite action that external
  repositories run with no pip install; that constraint extends here. The regularized
  incomplete beta function (for the exact binomial interval) is implemented directly, and
  `eval/test_stats.py` cross-checks it against SciPy when SciPy happens to be installed.
* **Deterministic.** Every resampling routine takes a seed and defaults to a fixed one, so
  a CI gate cannot flap because the bootstrap drew differently. Reruns reproduce the p-value
  digit for digit.
"""
from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist

__all__ = [
    "mean",
    "bootstrap_ci",
    "clopper_pearson",
    "paired_permutation_test",
    "PermutationResult",
    "holm",
    "design_analysis",
    "DesignAnalysis",
    "min_attainable_p",
    "required_queries",
]

_NORM = NormalDist()
DEFAULT_SEED = 20240917
DEFAULT_ITERS = 10_000
# Enumerate all 2^k sign assignments up to this many non-tied pairs; sample beyond it.
EXACT_PERMUTATION_LIMIT = 16


# --------------------------------------------------------------------------------------
# regularized incomplete beta — the only special function we need, and stdlib has no Beta
# --------------------------------------------------------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps, max_iter = 1e-30, 3e-16, 300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # even step
        num = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + num * d
        c = 1.0 + num / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        # odd step
        num = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + num * d
        c = 1.0 + num / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) — the Beta(a, b) CDF at x."""
    if not 0.0 <= x <= 1.0:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0.0 or x == 1.0:
        return x
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(log_front)
    # The continued fraction converges fast only on one side of the mean; reflect otherwise.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(p: float, a: float, b: float, tol: float = 1e-12) -> float:
    """Inverse Beta CDF by bisection — monotone, derivative-free, always converges."""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if p == 0.0 or p == 1.0:
        return p
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------------------
# interval estimation
# --------------------------------------------------------------------------------------

def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]. Input must be sorted."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[int(pos)]
    return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def bootstrap_ci(values, alpha: float = 0.05, iters: int = DEFAULT_ITERS,
                 seed: int = DEFAULT_SEED) -> tuple[float, float]:
    """Bias-corrected and accelerated (BCa) bootstrap interval for the mean.

    Falls back to the percentile interval when the acceleration is undefined — which
    happens exactly when every per-query value is identical (a degenerate sample, e.g.
    Recall@5 = 1.0 on every query). In that case the resample never varies and the
    interval collapses to the point estimate, which is *not* the same as certainty:
    reach for `clopper_pearson` when the metric is a 0/1 proportion.
    """
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    observed = mean(values)
    if n == 1 or len(set(values)) == 1:
        return (observed, observed)

    rng = random.Random(seed)
    boot = sorted(mean(rng.choices(values, k=n)) for _ in range(iters))

    below = sum(1 for b in boot if b < observed)
    if below in (0, iters):                     # z0 would be infinite — percentile instead
        return (_percentile(boot, alpha / 2), _percentile(boot, 1 - alpha / 2))
    z0 = _NORM.inv_cdf(below / iters)

    # jackknife acceleration
    total = sum(values)
    jack = [(total - v) / (n - 1) for v in values]
    jack_mean = mean(jack)
    num = sum((jack_mean - j) ** 3 for j in jack)
    den = 6.0 * (sum((jack_mean - j) ** 2 for j in jack) ** 1.5)
    a_hat = num / den if den else 0.0

    def adjust(z_q: float) -> float:
        denom = 1.0 - a_hat * (z0 + z_q)
        if denom == 0.0:
            return _NORM.cdf(z0 + z_q)
        return _NORM.cdf(z0 + (z0 + z_q) / denom)

    lo_q = adjust(_NORM.inv_cdf(alpha / 2))
    hi_q = adjust(_NORM.inv_cdf(1 - alpha / 2))
    return (_percentile(boot, lo_q), _percentile(boot, hi_q))


def clopper_pearson(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Clopper-Pearson) binomial interval — for per-query 0/1 metrics.

    This is the interval that makes `Recall@5 = 1.00` honest: 10 successes out of 10
    queries gives [0.69, 1.00] at 95%, not [1.00, 1.00]. Guaranteed >= nominal coverage
    (conservative by construction), and defined at the 0/n and n/n boundaries where the
    bootstrap and the normal approximation both fail.
    """
    if n <= 0:
        return (0.0, 1.0)
    if not 0 <= successes <= n:
        raise ValueError(f"successes must be in [0, {n}], got {successes}")
    lo = 0.0 if successes == 0 else beta_ppf(alpha / 2, successes, n - successes + 1)
    hi = 1.0 if successes == n else beta_ppf(1 - alpha / 2, successes + 1, n - successes)
    return (lo, hi)


# --------------------------------------------------------------------------------------
# hypothesis testing
# --------------------------------------------------------------------------------------

class PermutationResult:
    """Outcome of a paired randomization test, with the design facts that bound it."""

    __slots__ = ("p_value", "observed_diff", "n_pairs", "effective_n", "exact",
                 "iterations", "min_attainable_p")

    def __init__(self, p_value: float, observed_diff: float, n_pairs: int, effective_n: int,
                 exact: bool, iterations: int, min_attainable_p: float):
        self.p_value = p_value
        self.observed_diff = observed_diff
        self.n_pairs = n_pairs
        self.effective_n = effective_n      # pairs that actually differ; ties carry no signal
        self.exact = exact
        self.iterations = iterations
        self.min_attainable_p = min_attainable_p

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05

    @property
    def underpowered(self) -> bool:
        """True when no outcome of this test could have reached significance."""
        return self.min_attainable_p >= 0.05

    def as_dict(self) -> dict:
        return {
            "p_value": self.p_value,
            "observed_diff": self.observed_diff,
            "n_pairs": self.n_pairs,
            "effective_n": self.effective_n,
            "exact": self.exact,
            "iterations": self.iterations,
            "min_attainable_p": self.min_attainable_p,
            "underpowered": self.underpowered,
        }

    def __repr__(self) -> str:
        kind = "exact" if self.exact else f"MC({self.iterations})"
        return (f"PermutationResult(diff={self.observed_diff:+.4f}, p={self.p_value:.4f}, "
                f"{kind}, effective_n={self.effective_n}/{self.n_pairs})")


def min_attainable_p(effective_n: int) -> float:
    """Smallest two-sided p-value a paired sign-flip test on `effective_n` pairs can return.

    Every sign assignment is equally likely under the null, so the observed assignment and
    its full mirror image are always at least as extreme as themselves: p >= 2 / 2^k. With
    three non-tied queries that floor is 0.25 — such a comparison cannot reach significance
    no matter how large the effect, and reporting one as a "win" is reporting noise.
    """
    if effective_n <= 0:
        return 1.0
    return min(1.0, 2.0 ** (1 - effective_n))


def paired_permutation_test(a, b, iters: int = DEFAULT_ITERS, seed: int = DEFAULT_SEED,
                            exact_limit: int = EXACT_PERMUTATION_LIMIT) -> PermutationResult:
    """Two-sided paired randomization test on the mean difference (a - b).

    The null hypothesis is that the two systems are interchangeable per query, so flipping
    the sign of any query's difference is equally likely. Exact by enumerating all 2^k sign
    assignments when k (the non-tied pairs) is small enough; Monte Carlo with a fixed seed
    otherwise. Monte Carlo uses the add-one estimator (r + 1) / (iters + 1), which keeps the
    p-value strictly positive and valid under the null.

    No normality assumption, which matters: reciprocal rank is a discrete, heavily skewed
    variable that a t-test handles badly at small n.
    """
    a, b = [float(x) for x in a], [float(x) for x in b]
    if len(a) != len(b):
        raise ValueError(f"paired test needs equal lengths, got {len(a)} and {len(b)}")
    n = len(a)
    if n == 0:
        return PermutationResult(1.0, 0.0, 0, 0, True, 0, 1.0)

    diffs = [x - y for x, y in zip(a, b)]
    nonzero = [d for d in diffs if d != 0.0]
    k = len(nonzero)
    observed = sum(diffs) / n
    floor_p = min_attainable_p(k)

    if k == 0:                                   # identical on every query
        return PermutationResult(1.0, 0.0, n, 0, True, 0, 1.0)

    target = abs(sum(nonzero))                   # /n is a positive constant — compare sums
    tol = 1e-12 * max(1.0, target)

    if k <= exact_limit:
        extreme = sum(
            1 for signs in itertools.product((1.0, -1.0), repeat=k)
            if abs(sum(s * d for s, d in zip(signs, nonzero))) >= target - tol
        )
        return PermutationResult(extreme / (2 ** k), observed, n, k, True, 2 ** k, floor_p)

    rng = random.Random(seed)
    extreme = 0
    for _ in range(iters):
        total = 0.0
        for d in nonzero:
            total += d if rng.random() < 0.5 else -d
        if abs(total) >= target - tol:
            extreme += 1
    return PermutationResult((extreme + 1) / (iters + 1), observed, n, k, False, iters, floor_p)


def holm(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values, returned in the input order.

    Comparing five retrieval modes against one baseline is five tests; at an uncorrected
    0.05 the chance of at least one false "win" is about 23%. Holm controls the family-wise
    error rate at 0.05 while being uniformly more powerful than plain Bonferroni.
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (m - rank) * p_values[idx])   # enforce monotonicity
        adjusted[idx] = min(1.0, running)
    return adjusted


# --------------------------------------------------------------------------------------
# design analysis — what this gold set can resolve, before anyone runs it
# --------------------------------------------------------------------------------------

class DesignAnalysis:
    """What a gold set of size n can and cannot detect, stated up front."""

    __slots__ = ("n", "alpha", "power", "sd", "min_detectable_effect",
                 "min_attainable_p", "queries_for_target", "target_effect")

    def __init__(self, n: int, alpha: float, power: float, sd: float,
                 min_detectable_effect: float, floor_p: float,
                 queries_for_target: int | None, target_effect: float | None):
        self.n = n
        self.alpha = alpha
        self.power = power
        self.sd = sd
        self.min_detectable_effect = min_detectable_effect
        self.min_attainable_p = floor_p
        self.queries_for_target = queries_for_target
        self.target_effect = target_effect

    def as_dict(self) -> dict:
        return {
            "n": self.n, "alpha": self.alpha, "power": self.power,
            "sd_of_differences": self.sd,
            "min_detectable_effect": self.min_detectable_effect,
            "min_attainable_p": self.min_attainable_p,
            "target_effect": self.target_effect,
            "queries_for_target": self.queries_for_target,
        }


def required_queries(effect: float, sd: float, alpha: float = 0.05,
                     power: float = 0.80) -> int | None:
    """Paired-sample size needed to detect `effect` at the given alpha and power."""
    if effect <= 0 or sd <= 0:
        return None
    z = _NORM.inv_cdf(1 - alpha / 2) + _NORM.inv_cdf(power)
    return max(2, math.ceil((z * sd / effect) ** 2))


def design_analysis(a, b, alpha: float = 0.05, power: float = 0.80,
                    target_effect: float | None = None) -> DesignAnalysis:
    """Resolution of a paired comparison: smallest detectable effect, smallest p-value.

    Uses the observed per-query difference spread as the variance estimate, so it answers
    the question a small gold set actually raises — "could this comparison have found
    anything?" — rather than the one it pretends to answer.
    """
    a, b = [float(x) for x in a], [float(x) for x in b]
    n = len(a)
    diffs = [x - y for x, y in zip(a, b)]
    k = sum(1 for d in diffs if d != 0.0)
    if n < 2:
        return DesignAnalysis(n, alpha, power, 0.0, float("inf"), min_attainable_p(k),
                              None, target_effect)

    mu = mean(diffs)
    sd = math.sqrt(sum((d - mu) ** 2 for d in diffs) / (n - 1))
    z = _NORM.inv_cdf(1 - alpha / 2) + _NORM.inv_cdf(power)
    mde = z * sd / math.sqrt(n) if sd > 0 else 0.0
    needed = required_queries(target_effect, sd, alpha, power) if target_effect else None
    return DesignAnalysis(n, alpha, power, sd, mde, min_attainable_p(k), needed, target_effect)
