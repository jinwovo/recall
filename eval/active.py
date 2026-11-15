"""Asking for the labels that matter: active, anytime-valid certification (docs/adr/0018).

Every guarantee in this repository waits on the same missing input. The judge calibration
(ADR 0015) needs hand labels; the claim threshold (ADR 0017) needs hand labels; the drift
alarms (ADR 0016, 0017) watch the judge only because no true signal exists. The machinery to
turn labels into guarantees is all here. A way to get the labels cheaply is not.

The usual answer is "label a random sample", which spends most of the budget confirming
what the judge already got right. A judge that says SUPPORTED with confidence on an answer
that is plainly supported has nothing to learn from a human. The one that says PARTIAL is
where a label moves the estimate.

**Active statistical inference** (Zrnic & Candes, ICML 2024) makes that intuition valid.
Each answer gets a known inclusion probability ``pi_i`` set by the judge's uncertainty, a
coin with that bias decides whether a human sees it, and the estimate weights each label by
``1/pi_i``:

    Z_i = lam * f_i + (xi_i / pi_i) * (Y_i - lam * f_i)        theta_hat = mean(Z)

``f_i`` is the judge's prediction for every answer, ``Y_i`` the human label (only seen when
``xi_i = 1``), ``lam`` the power-tuning weight from PPI++. The second term is the judge's
error, measured where a human looked and reweighted to stand for every answer. Because
``E[xi_i / pi_i] = 1``, ``Z_i`` is unbiased for ``Y_i`` **whatever the judge says and however
the probabilities are chosen**. Choosing them well only changes the variance. Uniform
``pi`` is exactly prediction-powered inference, so ADR 0015 is the special case.

Three things are added here, and they are the point of this module.

1. **Robust sampling.** Uncertainty-driven sampling is only better when the uncertainty is
   honest. A judge that is *confidently wrong* gets its mistakes sampled at the floor rate,
   each carrying a huge weight, and the interval gets wider than uniform. Following
   arXiv 2511.08991, the design mixes the active rule with uniform sampling, and
   ``choose_mix`` picks the mix on a pilot sample. Uniform is a candidate, so on the pilot
   the chosen design cannot be worse than uniform.
2. **Anytime validity.** Serving labels a trickle of answers, and an operator will look at
   the number every morning. ``ActiveConfidenceSequence`` bets on the ``Z_i`` stream the way
   ``sequential.py`` bets on metrics (Waudby-Smith & Ramdas, 2024). ``pi_t`` and ``lam_t``
   are fixed before the coin is tossed, so ``Z_t`` has conditional mean ``theta``, and the
   interval holds at every day it is read. This is the setting of *Active, anytime-valid
   risk controlling prediction sets* (Xu, Karampatziakis & Mineiro, NeurIPS 2024),
   applied to a mean.
3. **Recalibrating the claim threshold from the same labels.** Actively chosen answers are
   *not* exchangeable with traffic. They over-represent whatever the judge found hard, and
   the strict conformal quantile computed on them answers a question about a different
   population. Weighting each calibration answer by ``1/pi_i`` is the likelihood ratio
   between traffic and the labelled sample, which is exactly the covariate-shift case of
   weighted conformal prediction (Tibshirani, Barber, Candes & Ramdas, NeurIPS 2019). The
   test point's weight is bounded by ``1/floor``, so a single threshold stays valid for
   every answer serving will see.

What this module cannot do: make a biased *label* honest. Validity is relative to the human
labels, and ``docs/LABELING-GUIDE.md`` is still the part that decides whether they are right.

Standard library only, deterministic given a seed.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

from factuality import Answer, FactualityCertificate, evaluate, nonconformity
from sequential import DEFAULT_ALPHA, HEDGE, BET_FRACTION

__all__ = [
    "judge_uncertainty",
    "ResidualModel",
    "inclusion_probabilities",
    "design_by_verdict",
    "choose_mix",
    "ActiveEstimate",
    "active_mean",
    "ActiveCapitalProcess",
    "ActiveConfidenceSequence",
    "asymptotic_radius",
    "weighted_conformal_quantile",
    "calibrate_strict_weighted",
    "DEFAULT_FLOOR",
    "DEFAULT_MIX",
]

_NORM = NormalDist()

#: Smallest inclusion probability ever used. It caps every weight at 1/floor, which is what
#: keeps the betting bounds finite and the weighted conformal test weight bounded. Below a
#: few percent, one unlucky label swings the estimate more than the design saves.
DEFAULT_FLOOR = 0.02
#: Share of the budget spent uniformly when no pilot has chosen otherwise. Half is the
#: conservative default from arXiv 2511.08991: at most twice uniform's variance if the
#: uncertainty is worthless, most of the active gain if it is good.
DEFAULT_MIX = 0.5
MIX_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
#: Stream length the asymptotic confidence sequence is tuned to be tightest at.
DEFAULT_T_STAR = 2000
#: Labels before the asymptotic sequence reports anything narrower than [0, 1].
DEFAULT_MIN_LABELS = 60
#: The judge's three verdicts as serving scores them (Judgment.Verdict), and the config key
#: suffix each one's inclusion probability is set under.
VERDICT_SCORES = {1.0: "supported", 0.5: "partial", 0.0: "unsupported"}


# --------------------------------------------------------------------------------------
# the design: who gets labelled, with what probability
# --------------------------------------------------------------------------------------

def judge_uncertainty(prediction: float) -> float:
    """The textbook rule: the label's expected effect, *if the judge were calibrated*.

    For a binary label and a calibrated prediction ``f``, ``E[(Y - f)^2] = f (1 - f)``, and
    the variance-optimal inclusion probability is proportional to its square root (Zrnic &
    Candes, section 3). For this system's three-level judge that is 0 for SUPPORTED and
    UNSUPPORTED and 0.5 for PARTIAL, which says a confident verdict is never wrong.

    It is kept because it is the rule a reader will reach for first, and the experiment
    shows what it costs: a judge that is confidently wrong one time in ten sends those
    answers to review at the floor rate, each label weighs 1/floor, and the interval comes
    out *wider* than uniform sampling. ``ResidualModel`` learns the error instead.
    """
    f = min(1.0, max(0.0, prediction))
    return math.sqrt(f * (1.0 - f))


class ResidualModel:
    """The judge's root-mean-squared error per verdict, learned from labelled answers.

    The judge emits three values, so the model is three numbers, each shrunk toward
    ``prior_mse`` (0.25, the most a binary label can miss by on average) with
    ``prior_weight`` pseudo-labels. With no labels at all every verdict gets the prior and
    the design is uniform, which is the right cold start: nothing is known yet about where
    the judge errs.
    """

    def __init__(self, prior_mse: float = 0.25, prior_weight: float = 2.0):
        self.prior_mse = prior_mse
        self.prior_weight = prior_weight
        self._sq: dict[float, float] = {}
        self._w: dict[float, float] = {}
        self._n: dict[float, int] = {}

    @staticmethod
    def level(prediction: float) -> float:
        return round(min(1.0, max(0.0, prediction)), 3)

    def fit(self, pred: list[float], labels: list[float],
            weights: list[float] | None = None) -> "ResidualModel":
        """``weights`` (1/pi) make an actively drawn sample count as traffic would."""
        weights = weights or [1.0] * len(pred)
        for f, y, w in zip(pred, labels, weights):
            k = self.level(f)
            self._sq[k] = self._sq.get(k, 0.0) + w * (y - f) ** 2
            self._w[k] = self._w.get(k, 0.0) + w
            self._n[k] = self._n.get(k, 0) + 1
        return self

    def mse(self, prediction: float) -> float:
        k = self.level(prediction)
        n = self._n.get(k, 0)
        if not n:
            return self.prior_mse
        # Within a verdict the weights are nearly equal, so the weighted mean is the
        # verdict's error rate; the prior counts as `prior_weight` labels against `n` real ones.
        data = self._sq[k] / self._w[k]
        return (self.prior_weight * self.prior_mse + n * data) / (self.prior_weight + n)

    def uncertainty(self, prediction: float) -> float:
        return math.sqrt(self.mse(prediction))


def _check_design(budget: float, floor: float, mix: float) -> None:
    if not 0.0 < budget <= 1.0:
        raise ValueError(f"budget must be in (0, 1], got {budget}")
    if not 0.0 < floor <= budget:
        raise ValueError(f"floor must be in (0, budget], got {floor} with budget {budget}")
    if not 0.0 <= mix <= 1.0:
        raise ValueError(f"mix must be in [0, 1], got {mix}")


def _scale_to_budget(uncertainty: list[float], budget: float,
                     weights: list[float] | None = None) -> list[float]:
    """min(1, c * u_i) with c chosen so the (weighted) mean is the budget. Bisection: the
    mean is continuous and non-decreasing in c, and capping at 1 is why there is no closed
    form."""
    if not uncertainty or max(uncertainty) <= 0.0:
        return [budget] * len(uncertainty)
    w = weights or [1.0] * len(uncertainty)
    total = sum(w)

    def spend(c: float) -> float:
        return sum(wi * min(1.0, c * u) for wi, u in zip(w, uncertainty)) / total

    if spend(1e12) < budget:
        # Fewer uncertain items than the budget covers: label all of them.
        return [1.0 if u > 0 else 0.0 for u in uncertainty]
    lo, hi = 0.0, 1.0
    while spend(hi) < budget:
        hi *= 2.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if spend(mid) < budget:
            lo = mid
        else:
            hi = mid
    return [min(1.0, hi * u) for u in uncertainty]


def inclusion_probabilities(uncertainty: list[float], budget: float,
                            floor: float = DEFAULT_FLOOR, mix: float = DEFAULT_MIX,
                            weights: list[float] | None = None) -> list[float]:
    """Per-item labelling probabilities that spend ``budget`` (a share of items) on average.

    ``mix`` is the share spent uniformly: 0 is fully active, 1 is uniform sampling (PPI).
    ``weights`` say how many traffic items each entry stands for (a per-verdict design
    passes the verdict counts). The floor is applied last, so the realised spend can sit
    slightly above the budget when many items are confident; the excess is at most ``floor``.
    """
    _check_design(budget, floor, mix)
    active = _scale_to_budget(list(uncertainty), budget, weights)
    return [min(1.0, max(floor, (1.0 - mix) * a + mix * budget)) for a in active]


def choose_mix(pilot_pred: list[float], pilot_labels: list[float], budget: float,
               floor: float = DEFAULT_FLOOR, mixes=MIX_GRID,
               uncertainty=judge_uncertainty, weights: list[float] | None = None) -> float:
    """Pick the uniform share from a pilot sample with both a judge prediction and a label.

    For an item labelled with probability ``pi``, the IPW term adds ``(Y - f)^2 (1/pi - 1)``
    to the variance. Its average over the pilot is the part of the variance the design
    controls, and it is evaluated for every mix on the grid. Because ``mix = 1`` (uniform)
    is on the grid, the chosen design is no worse than uniform *on the pilot*. That is the
    robustness the active rule lacks on its own (arXiv 2511.08991).

    A pilot that was itself drawn actively is not a sample of traffic; pass ``weights``
    (1/pi of each pilot item) and both the budget and the cost are averaged as traffic
    would weight them.
    """
    if len(pilot_pred) != len(pilot_labels) or not pilot_pred:
        raise ValueError("the pilot needs aligned, non-empty predictions and labels")
    u = [uncertainty(f) for f in pilot_pred]
    sq = [(y - f) ** 2 for y, f in zip(pilot_labels, pilot_pred)]
    w = weights if weights is not None else [1.0] * len(sq)
    best, best_cost = 1.0, math.inf
    for mix in sorted(mixes, reverse=True):        # ties go to the more uniform design
        pis = inclusion_probabilities(u, budget, floor, mix, weights=w)
        cost = sum(wi * e * (1.0 / p - 1.0) for wi, e, p in zip(w, sq, pis)) / sum(w)
        if cost < best_cost - 1e-12:
            best, best_cost = mix, cost
    return best


def design_by_verdict(pred: list[float], pi: list[float], selected: list[bool], labels: list,
                      budget: float, floor: float = DEFAULT_FLOOR) -> dict:
    """Next period's per-verdict inclusion probabilities, from this period's review export.

    Serving looks the probability up by the judge's verdict, so the design *is* three
    numbers. The residual model is fitted on the labelled answers weighted back to traffic,
    the mix is chosen the same way, and the budget is spent against the verdict shares of
    all judged traffic. Whatever this returns, next period's estimates stay valid: the
    probabilities are fixed before any of next period's coins are tossed.
    """
    lab_f = [f for f, s in zip(pred, selected) if s]
    lab_y = [y for y, s in zip(labels, selected) if s]
    lab_w = [1.0 / p for p, s in zip(pi, selected) if s]
    model = ResidualModel().fit(lab_f, lab_y, lab_w)
    mix = (choose_mix(lab_f, lab_y, budget, floor, uncertainty=model.uncertainty,
                      weights=lab_w) if lab_f else 1.0)
    shares: dict[float, float] = {}
    for f in pred:
        k = ResidualModel.level(f)
        shares[k] = shares.get(k, 0.0) + 1.0
    levels = sorted(shares)
    pis = inclusion_probabilities([model.uncertainty(k) for k in levels], budget, floor, mix,
                                  weights=[shares[k] for k in levels])
    return {"mix": mix, "pi": dict(zip(levels, pis)),
            "rmse": {k: model.uncertainty(k) for k in levels},
            "share": {k: shares[k] / len(pred) for k in levels}}


# --------------------------------------------------------------------------------------
# a fixed-sample estimate
# --------------------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _cov(a, b):
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = _mean(a), _mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)


def _terms(pred, pi, selected, labels):
    """Split Z_i into lam * A_i + B_i, so the variance is a quadratic in lam."""
    a, b = [], []
    for f, p, xi, y in zip(pred, pi, selected, labels):
        if not 0.0 < p <= 1.0:
            raise ValueError(f"inclusion probabilities must be in (0, 1], got {p}")
        if xi and y is None:
            raise ValueError("a selected item has no label; label it or drop the export")
        w = (1.0 / p) if xi else 0.0
        a.append(f * (1.0 - w))
        b.append(w * (y if xi else 0.0))
    return a, b


class ActiveEstimate:
    """The true rate, from the judge on every item and a human on an active sample."""

    __slots__ = ("estimate", "lo", "hi", "alpha", "lam", "n_items", "n_labels",
                 "judge_only", "variance", "equivalent_uniform_labels")

    def __init__(self, estimate, lo, hi, alpha, lam, n_items, n_labels, judge_only,
                 variance, equivalent_uniform_labels):
        self.estimate = estimate
        self.lo = lo
        self.hi = hi
        self.alpha = alpha
        self.lam = lam
        self.n_items = n_items
        self.n_labels = n_labels
        self.judge_only = judge_only
        self.variance = variance
        # Hand labels a judge-free random sample would need for the same precision.
        self.equivalent_uniform_labels = equivalent_uniform_labels

    @property
    def width(self) -> float:
        return self.hi - self.lo

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__} | {"width": self.width}

    def statement(self) -> str:
        return (f"The judge reports {self.judge_only:.3f} over {self.n_items} answers. "
                f"From {self.n_labels} actively chosen human labels the true rate is "
                f"{self.estimate:.3f} [{self.lo:.3f}, {self.hi:.3f}] "
                f"({1 - self.alpha:.0%}), as precise as about "
                f"{self.equivalent_uniform_labels:.0f} labels drawn at random without a judge.")


def active_mean(pred: list[float], pi: list[float], selected: list[bool],
                labels: list, alpha: float = DEFAULT_ALPHA, lam: float | None = None
                ) -> ActiveEstimate:
    """Confidence interval for the mean label over all items, from an active sample.

    ``labels[i]`` is read only where ``selected[i]`` is true (use None elsewhere). ``pi[i]``
    must be the probability the item *had* of being selected, fixed before its coin was
    tossed. A probability reconstructed afterwards from the label voids the estimate.
    Leave ``lam`` None to power-tune it (clipped to [0, 1]).
    """
    n = len(pred)
    if not (n == len(pi) == len(selected) == len(labels)):
        raise ValueError("pred, pi, selected and labels must be the same length")
    if n < 2:
        raise ValueError("need at least 2 items")
    a, b = _terms(pred, pi, selected, labels)
    if lam is None:
        var_a = _var(a)
        lam = 0.0 if var_a <= 0.0 else min(1.0, max(0.0, -_cov(a, b) / var_a))
    z = [lam * ai + bi for ai, bi in zip(a, b)]
    est = _mean(z)
    variance = _var(z) / n
    half = _NORM.inv_cdf(1 - alpha / 2) * math.sqrt(variance)
    p = min(1.0, max(0.0, est))
    equivalent = (p * (1 - p) / variance) if variance > 0 else float(sum(selected))
    return ActiveEstimate(est, est - half, est + half, alpha, lam, n, sum(1 for s in selected if s),
                          _mean(pred), variance, equivalent)


# --------------------------------------------------------------------------------------
# anytime-valid: the estimate an operator may read every day
# --------------------------------------------------------------------------------------

def _z_range(f: float, p: float, lam: float) -> tuple[float, float]:
    """Every value Z_t can take, known before the coin and the label: the bet sizes are
    capped by these, which is what keeps wealth non-negative."""
    base = lam * f
    values = [base + (0.0 - base) / p, base + (1.0 - base) / p]
    if p < 1.0:
        values.append(base)
    return min(values), max(values)


class ActiveCapitalProcess:
    """Wealth from betting that the mean of the Z stream is ``m``.

    Same construction as ``sequential.CapitalProcess``, with two changes. Z is not in
    [0, 1] (a label weighted by 1/pi can be large), so each bet is capped by the range Z can
    take *at this step*. And the variance the stake adapts to is the Z stream's own.
    """

    __slots__ = ("m", "alpha", "n", "_log_up", "_log_down", "_sum", "_sq_dev", "_running",
                 "_var", "_log_reject")

    def __init__(self, m: float, alpha: float = DEFAULT_ALPHA):
        if not 0.0 < m < 1.0:
            raise ValueError(f"hypothesised mean must be in (0, 1), got {m}")
        self.m = m
        self.alpha = alpha
        self.n = 0
        self._log_up = 0.0
        self._log_down = 0.0
        self._sum = 0.0
        self._sq_dev = 0.0
        self._running = 0.5
        self._var = 0.25
        self._log_reject = math.log(1.0 / (alpha * HEDGE))

    def update(self, z: float, z_min: float, z_max: float) -> "ActiveCapitalProcess":
        t = self.n + 1
        stake = math.sqrt(2.0 * math.log(2.0 / self.alpha) / (self._var * t * math.log(1.0 + t)))
        edge = z - self.m
        up = stake if z_min >= self.m else min(stake, BET_FRACTION / (self.m - z_min))
        down = stake if z_max <= self.m else min(stake, BET_FRACTION / (z_max - self.m))
        self._log_up += math.log1p(up * edge)
        self._log_down += math.log1p(-down * edge)
        self.n = t
        self._sum += z
        self._sq_dev += (z - self._running) ** 2
        self._running = (0.5 + self._sum) / (1.0 + t)
        self._var = (0.25 + self._sq_dev) / (1.0 + t)
        return self

    @property
    def rejected(self) -> bool:
        return max(self._log_up, self._log_down) >= self._log_reject


class ActiveConfidenceSequence:
    """The true groundedness rate, valid at every point of a live labelling stream.

    Feed every judged answer, in arrival order, through ``observe``: the judge's prediction,
    the probability it had of being sent for review, whether it was, and its label if it
    has one. ``lam`` is re-estimated from labelled answers seen so far, which keeps it
    predictable. It starts at 1, the plain PPI rectifier, until there are enough labels to
    tune it.
    """

    def __init__(self, alpha: float = DEFAULT_ALPHA, grid: int = 200, min_tune: int = 20):
        self.alpha = alpha
        self._points = [(j + 0.5) / grid for j in range(grid)]
        self._books = [ActiveCapitalProcess(m, alpha) for m in self._points]
        self._pairs: list[tuple[float, float]] = []
        self._min_tune = min_tune
        self.n = 0
        self.n_labels = 0
        self._z_sum = 0.0
        self._z_sq = 0.0

    @property
    def lam(self) -> float:
        if len(self._pairs) < self._min_tune:
            return 1.0
        ys = [y for y, _ in self._pairs]
        fs = [f for _, f in self._pairs]
        var_f = _var(fs)
        return 1.0 if var_f <= 0 else min(1.0, max(0.0, _cov(ys, fs) / var_f))

    def observe(self, pred: float, pi: float, selected: bool, label: float | None = None
                ) -> tuple[float, float]:
        if not 0.0 < pi <= 1.0:
            raise ValueError(f"inclusion probability must be in (0, 1], got {pi}")
        if selected and label is None:
            raise ValueError("a selected answer must carry its label")
        lam = self.lam                                 # fixed before this item's label
        f = min(1.0, max(0.0, pred))
        z_min, z_max = _z_range(f, pi, lam)
        z = lam * f + ((label - lam * f) / pi if selected else 0.0)
        for book in self._books:
            book.update(z, z_min, z_max)
        if selected:
            self._pairs.append((float(label), f))
            self.n_labels += 1
        self.n += 1
        self._z_sum += z
        self._z_sq += z * z
        return self.bounds

    @property
    def estimate(self) -> float:
        return self._z_sum / self.n if self.n else math.nan

    @property
    def bounds(self) -> tuple[float, float]:
        """Exact: valid at every n with no approximation, and wide for it. Each bet must
        survive a label weighted 1/pi, so with small inclusion probabilities the bets stay
        small and the interval narrows slowly."""
        alive = [m for m, b in zip(self._points, self._books) if not b.rejected]
        return (min(alive), max(alive)) if alive else (0.0, 1.0)

    def asymptotic_bounds(self, t_star: int = DEFAULT_T_STAR,
                          min_labels: int = DEFAULT_MIN_LABELS) -> tuple[float, float]:
        """Asymptotically anytime-valid: the Gaussian-mixture boundary of Waudby-Smith,
        Arbour, Sinha, Kennedy & Ramdas (arXiv 2103.06476), around the running mean
        of Z. It needs a CLT where the betting bound needs none, and an IPW stream is the
        worst case for one: until a few dozen labels have arrived, the variance estimate has
        seen almost none of the 1/pi-weighted terms that dominate it. Read from the first
        answer, the simulation misses the truth in 30-70% of runs against a 5% promise; read
        from the 30th label, up to 8%; from the 60th, at most 4.3%. So it reports [0, 1]
        until ``min_labels``, and ``bounds`` is the one to quote before then."""
        if self.n < 2 or self.n_labels < min_labels:
            return (0.0, 1.0)
        mean = self._z_sum / self.n
        var = max(0.0, (self._z_sq - self.n * mean * mean) / (self.n - 1))
        half = math.sqrt(var) * asymptotic_radius(self.n, self.alpha, t_star)
        return (max(0.0, mean - half), min(1.0, mean + half))


def asymptotic_radius(n: int, alpha: float, t_star: int = DEFAULT_T_STAR) -> float:
    """Half-width of the two-sided asymptotic confidence sequence, in standard deviations.

    rho is tuned so the boundary is tightest near ``t_star`` answers. It must be fixed before
    the stream starts; choosing it from the data would void the time-uniform guarantee.
    """
    a = -2.0 * math.log(alpha)
    rho2 = (a + math.log(a + 1.0)) / t_star
    return math.sqrt(2.0 * (n * rho2 + 1.0) / (n * n * rho2)
                     * math.log(math.sqrt(n * rho2 + 1.0) / alpha))


# --------------------------------------------------------------------------------------
# the claim threshold, recalibrated on an active sample
# --------------------------------------------------------------------------------------

def weighted_conformal_quantile(scores: list[float], weights: list[float], alpha: float,
                                test_weight: float) -> float:
    """The (1 - alpha) quantile of the weighted empirical distribution with a point mass at
    +inf carrying the test point's weight (Tibshirani et al. 2019, eq. 7).

    With equal weights this is ``conformal.conformal_quantile`` exactly. +inf means the
    weighted sample is too small to promise anything at this alpha.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if len(scores) != len(weights):
        raise ValueError("scores and weights must align")
    if any(w <= 0 for w in weights) or test_weight <= 0:
        raise ValueError("weights must be positive")
    total = sum(weights) + test_weight
    target = (1.0 - alpha) * total
    acc = 0.0
    for s, w in sorted(zip(scores, weights)):
        acc += w
        if acc >= target - 1e-12 * total:
            return s
    return math.inf


def calibrate_strict_weighted(answers: list[Answer], pis: list[float], alpha: float = 0.10,
                              floor: float = DEFAULT_FLOOR) -> FactualityCertificate:
    """The ADR 0017 strict promise, from answers chosen with known probabilities ``pis``.

    Each calibration answer is weighted 1/pi, the ratio between how often traffic produces
    it and how often the design labelled it. The test answer's weight is unknown until it
    arrives, so the bound 1/floor is used: conservative, and one threshold for all traffic.
    """
    if len(answers) != len(pis):
        raise ValueError("answers and pis must align")
    if any(not floor <= p <= 1.0 for p in pis):
        raise ValueError(f"every pi must lie in [floor={floor}, 1]")
    tau = weighted_conformal_quantile([nonconformity(a) for a in answers],
                                      [1.0 / p for p in pis], alpha, 1.0 / floor)
    return FactualityCertificate(tau, "all-supported", alpha, len(answers),
                                 evaluate(answers, tau))


# --------------------------------------------------------------------------------------
# CLI: an export from serving's review queue -> the two numbers worth publishing
# --------------------------------------------------------------------------------------

def load_review_export(path: str):
    """One JSON object per judged answer, as ``/api/admin/review/export`` writes it:

        {"prediction": 0.5, "pi": 0.31, "selected": true, "supported": false,
         "claims": [{"score": 0.82, "supported": true}, ...]}

    Unselected rows carry only prediction and pi. A selected row without a label is an
    error, not a skip: dropping the hard rows nobody finished is the bias this design exists
    to remove.
    """
    import json

    pred, pi, sel, lab, claim_rows = [], [], [], [], []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            s = bool(row.get("selected"))
            y = row.get("supported")
            if s and not isinstance(y, bool):
                raise ValueError(f"{path}:{lineno}: selected for review but not labelled")
            pred.append(float(row["prediction"]))
            pi.append(float(row["pi"]))
            sel.append(s)
            lab.append(float(y) if s else None)
            claims = row.get("claims")
            if s and claims and all(isinstance(c.get("supported"), bool)
                                    and c.get("score") is not None for c in claims):
                from factuality import Claim
                claim_rows.append(([Claim(c["score"], c["supported"]) for c in claims],
                                   float(row["pi"])))
    return pred, pi, sel, lab, claim_rows


def main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Certify from the review queue (docs/adr/0018).")
    ap.add_argument("export", help="JSONL from /api/admin/review/export")
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                    help="miscoverage of the groundedness interval")
    ap.add_argument("--claim-alpha", type=float, default=0.10)
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    ap.add_argument("--budget", type=float, default=None,
                    help="also recommend a uniform mix for this budget from the labels")
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    pred, pi, sel, lab, claim_rows = load_review_export(args.export)
    out = {}
    if sum(sel) < 2:
        print(f"{len(pred)} judged answers, {sum(sel)} labelled — not enough to estimate.")
        return 1

    fixed = active_mean(pred, pi, sel, lab, alpha=args.alpha)
    print(fixed.statement())
    out["fixed"] = fixed.as_dict()

    cs = ActiveConfidenceSequence(alpha=args.alpha)
    for f, p, s, y in zip(pred, pi, sel, lab):
        cs.observe(f, p, s, y)
    lo, hi = cs.bounds
    alo, ahi = cs.asymptotic_bounds()
    print(f"Anytime-valid ({1 - args.alpha:.0%}, safe to re-read as labels arrive): "
          f"exact [{lo:.3f}, {hi:.3f}], asymptotic [{alo:.3f}, {ahi:.3f}].")
    out["anytime"] = {"lo": lo, "hi": hi, "asymptotic_lo": alo, "asymptotic_hi": ahi,
                      "n": cs.n, "n_labels": cs.n_labels}

    if claim_rows:
        cert = calibrate_strict_weighted([a for a, _ in claim_rows], [p for _, p in claim_rows],
                                         alpha=args.claim_alpha, floor=args.floor)
        print(cert.statement())
        out["claims"] = cert.as_dict()
        if cert.usable:
            t = cert.threshold if math.isfinite(cert.threshold) else 0.0
            print(f"\nrecall.rag.factuality:\n  enabled: true\n  alpha: {args.claim_alpha}"
                  f"\n  threshold: {t}")

    if args.budget:
        # Next period's design from this period's labels. Valid whatever it says: serving
        # fixes the probabilities before any of next period's coins are tossed.
        design = design_by_verdict(pred, pi, sel, lab, args.budget, args.floor)
        by_name = {VERDICT_SCORES[k]: v for k, v in design["pi"].items() if k in VERDICT_SCORES}
        print(f"\nNext period's design for a {args.budget:.0%} label budget "
              f"(uniform share {design['mix']}):")
        print("recall.rag.review:\n  enabled: true")
        for name in ("supported", "partial", "unsupported"):
            if name in by_name:
                print(f"  pi-{name}: {by_name[name]:.4f}")
        out["design"] = {"mix": design["mix"],
                         "pi": {VERDICT_SCORES.get(k, str(k)): v for k, v in design["pi"].items()},
                         "rmse": {VERDICT_SCORES.get(k, str(k)): v
                                  for k, v in design["rmse"].items()}}

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
