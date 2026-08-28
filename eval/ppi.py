"""Making a cheap, biased judge produce a valid number (docs/adr/0015).

Every quality claim in this repository that involves an LLM rests on an LLM's opinion of
itself. `groundedness = 0.81` is the average score a judge model gave its own system's
answers; the relevance labels behind a large gold set would, at any real scale, be written
by a model too. Both are cheap and neither is trustworthy on its own — a judge that is
optimistic by eight points produces a number that is wrong by eight points, and nothing in
the pipeline notices.

The usual responses are both bad. Reporting the judge's average pretends a biased estimate
is a measurement. Labelling everything by hand is correct and does not scale past a few
dozen items.

**Prediction-powered inference** (Angelopoulos, Bates, Fannjiang, Jordan & Zrnic,
*Science*, 2023; power-tuned as PPI++, Angelopoulos, Bates & Zrnic, 2023) is the third
option: label a small sample by hand, let the model predict everything, and combine them
into a confidence interval for the **true** quantity that is

* **valid no matter how bad the model is** — bias is measured on the labelled sample and
  subtracted, so no assumption about the judge is needed, and
* **narrower than using the hand labels alone** whenever the model carries signal.

The estimator, with `Y` the hand labels, `f` the model's predictions, `n` labelled and `N`
unlabelled items:

    theta = mean(Y) + lambda * ( mean_unlabelled(f) - mean_labelled(f) )

The bracket is the model's *bias*, estimated on the labelled sample. `lambda` is tuned to
minimise variance, which is what makes this safe: a judge that predicts noise drives
`lambda` to zero and the estimator collapses to the hand-label mean, so **a useless model
costs nothing**. A good one buys effective sample size — the report says how many hand
labels alone would have bought the same precision.

Standard library only, deterministic. `test_ppi.py` checks the guarantee the way it has to
be checked: repeatedly, against a deliberately biased judge, counting how often the
interval misses.
"""
from __future__ import annotations

import math
from statistics import NormalDist

__all__ = ["PPIEstimate", "ppi_mean", "optimal_lambda", "classical_interval",
           "effective_sample_size"]

_NORM = NormalDist()
DEFAULT_ALPHA = 0.05


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _variance(values: list[float]) -> float:
    """Sample variance with Bessel's correction; zero for a degenerate sample."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = _mean(values)
    return sum((v - mu) ** 2 for v in values) / (n - 1)


def _covariance(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    mu_a, mu_b = _mean(a), _mean(b)
    return sum((x - mu_a) * (y - mu_b) for x, y in zip(a, b)) / (n - 1)


def optimal_lambda(labeled_true: list[float], labeled_pred: list[float],
                   n_unlabeled: int) -> float:
    """The power-tuning weight that minimises the estimator's variance (PPI++).

    Balances what the model adds against the noise it brings:

        lambda* = Cov(Y, f) / ( Var(f) * (1 + n/N) )

    Clipped to [0, 1]. Zero means the model is ignored entirely and the estimator is the
    ordinary hand-label mean — which is exactly what should happen when the predictions
    carry no information, and is why using a bad judge cannot hurt. One recovers the
    original (untuned) PPI estimator.
    """
    n = len(labeled_true)
    if n < 2 or n_unlabeled < 1:
        return 0.0
    variance_pred = _variance(labeled_pred)
    if variance_pred <= 0.0:                 # a constant prediction explains nothing
        return 0.0
    lam = _covariance(labeled_true, labeled_pred) / (variance_pred * (1.0 + n / n_unlabeled))
    return min(1.0, max(0.0, lam))


def classical_interval(values: list[float], alpha: float = DEFAULT_ALPHA) -> tuple[float, float]:
    """Normal-approximation interval from hand labels alone — the honest baseline."""
    n = len(values)
    if n < 2:
        return (0.0, 1.0)
    half = _NORM.inv_cdf(1 - alpha / 2) * math.sqrt(_variance(values) / n)
    return (_mean(values) - half, _mean(values) + half)


def effective_sample_size(labeled_true: list[float], variance_ppi: float) -> float:
    """Hand labels that would have bought this precision on their own.

    The hand-label-only estimator has variance Var(Y)/n, so matching a PPI variance of V
    needs Var(Y)/V labels. Reporting it turns "the interval got narrower" into a number an
    annotation budget can be argued with.
    """
    variance_true = _variance(labeled_true)
    if variance_ppi <= 0.0 or variance_true <= 0.0:
        return float(len(labeled_true))
    return variance_true / variance_ppi


class PPIEstimate:
    """A prediction-powered estimate, next to the two things it is better than."""

    __slots__ = ("estimate", "lo", "hi", "alpha", "lam", "n_labeled", "n_unlabeled",
                 "judge_only", "judge_bias", "labeled_only", "labeled_only_ci",
                 "effective_n", "variance")

    def __init__(self, estimate: float, lo: float, hi: float, alpha: float, lam: float,
                 n_labeled: int, n_unlabeled: int, judge_only: float, judge_bias: float,
                 labeled_only: float, labeled_only_ci: tuple[float, float],
                 effective_n: float, variance: float):
        self.estimate = estimate
        self.lo = lo
        self.hi = hi
        self.alpha = alpha
        self.lam = lam
        self.n_labeled = n_labeled
        self.n_unlabeled = n_unlabeled
        # What the judge claims on its own — no validity, and the number most reports quote.
        self.judge_only = judge_only
        # Measured on the labelled sample: positive means the judge flatters the system.
        self.judge_bias = judge_bias
        self.labeled_only = labeled_only
        self.labeled_only_ci = labeled_only_ci
        self.effective_n = effective_n
        self.variance = variance

    @property
    def width(self) -> float:
        return self.hi - self.lo

    @property
    def labeled_only_width(self) -> float:
        return self.labeled_only_ci[1] - self.labeled_only_ci[0]

    @property
    def narrower_than_labels_alone(self) -> bool:
        return self.width < self.labeled_only_width

    @property
    def judge_is_optimistic(self) -> bool:
        return self.judge_bias > 0

    def as_dict(self) -> dict:
        return {
            "estimate": self.estimate, "lo": self.lo, "hi": self.hi, "width": self.width,
            "alpha": self.alpha, "lambda": self.lam,
            "n_labeled": self.n_labeled, "n_unlabeled": self.n_unlabeled,
            "judge_only": self.judge_only, "judge_bias": self.judge_bias,
            "labeled_only": self.labeled_only,
            "labeled_only_ci": list(self.labeled_only_ci),
            "labeled_only_width": self.labeled_only_width,
            "effective_n": self.effective_n,
        }

    def statement(self) -> str:
        direction = "optimistic" if self.judge_bias > 0 else "pessimistic"
        return (
            f"The judge reports {self.judge_only:.3f}. Measured against {self.n_labeled} "
            f"hand labels it is {direction} by {abs(self.judge_bias):.3f}, so the true "
            f"value is {self.estimate:.3f} [{self.lo:.3f}, {self.hi:.3f}] — an interval "
            f"worth about {self.effective_n:.0f} hand labels, from {self.n_labeled}."
        )

    def __repr__(self) -> str:
        return (f"PPIEstimate({self.estimate:.4f} [{self.lo:.4f}, {self.hi:.4f}], "
                f"lambda={self.lam:.3f}, n={self.n_labeled}+{self.n_unlabeled})")


def ppi_mean(labeled_true: list[float], labeled_pred: list[float],
             unlabeled_pred: list[float], alpha: float = DEFAULT_ALPHA,
             lam: float | None = None) -> PPIEstimate:
    """Confidence interval for the true mean, from a few hand labels and many predictions.

    `labeled_true` and `labeled_pred` are the hand label and the model's prediction for the
    *same* items; `unlabeled_pred` holds predictions for items nobody labelled. The two
    groups must be **disjoint samples from the same population** — reusing labelled items in
    the unlabelled set correlates the two terms and voids the interval.

    Pass `lam=1.0` for the original PPI estimator, `lam=0.0` to ignore the model entirely,
    or leave it None to power-tune (recommended: it is never worse than either).
    """
    if len(labeled_true) != len(labeled_pred):
        raise ValueError(f"labelled pairs must align: {len(labeled_true)} labels "
                         f"against {len(labeled_pred)} predictions")
    n, N = len(labeled_true), len(unlabeled_pred)
    if n < 2:
        raise ValueError("prediction-powered inference needs at least 2 hand labels")
    if N < 1:
        raise ValueError("no unlabelled predictions — use classical_interval instead")

    weight = optimal_lambda(labeled_true, labeled_pred, N) if lam is None else lam
    bias = _mean(labeled_pred) - _mean(labeled_true)
    estimate = _mean(labeled_true) + weight * (_mean(unlabeled_pred) - _mean(labeled_pred))

    # Var(Y - lambda*f)/n covers the labelled term; lambda^2 Var(f)/N covers the unlabelled
    # one. The rectifier is what buys the precision: when f tracks Y, the difference has
    # much smaller variance than Y alone.
    residual = [y - weight * f for y, f in zip(labeled_true, labeled_pred)]
    variance = _variance(residual) / n + (weight ** 2) * _variance(unlabeled_pred) / N
    half = _NORM.inv_cdf(1 - alpha / 2) * math.sqrt(variance) if variance > 0 else 0.0

    return PPIEstimate(
        estimate=estimate, lo=estimate - half, hi=estimate + half, alpha=alpha,
        lam=weight, n_labeled=n, n_unlabeled=N,
        judge_only=_mean(unlabeled_pred + labeled_pred), judge_bias=bias,
        labeled_only=_mean(labeled_true),
        labeled_only_ci=classical_interval(labeled_true, alpha),
        effective_n=effective_sample_size(labeled_true, variance), variance=variance)
