"""Distribution-free risk control for RAG decisions (docs/adr/0013).

Every consequential number in a RAG pipeline is currently a constant somebody tuned.
Retrieve the top 8 passages — why 8? Abstain when the reranker's top score is below 0.35 —
why 0.35? These are the decisions that determine whether the system hallucinates and what
it costs to run, and they are set by the least rigorous mechanism in the codebase.

This module replaces them with thresholds carrying a **finite-sample, distribution-free
guarantee**: calibrate on held-out queries, and the deployed threshold provably meets a
stated error rate on unseen ones. No assumption about the score distribution, no asymptotic
hand-waving, no requirement that the model be calibrated — only that calibration and
deployment queries are exchangeable.

Two guarantees, for the two decisions:

**Coverage — how many passages to retrieve.** Split conformal prediction (Vovk et al.;
Papadopoulos et al. 2002) with adaptive set sizes (Romano, Sesia & Candès, NeurIPS 2020).
Instead of a fixed top-K, accumulate reranker mass until a calibrated level is reached: an
easy query where one passage dominates gets a short context, an ambiguous one gets a long
one, and

    P(the retrieved set contains a relevant document) >= 1 - alpha

holds for the next query, exactly, at any sample size. The payoff is direct — passages are
LLM context, so a smaller average set is fewer prompt tokens, lower cost and faster prefill,
bought without giving up a coverage promise a fixed K never made in the first place.

**Risk — when to refuse to answer.** Risk-controlling prediction sets (Bates, Angelopoulos,
Lei, Malik & Jordan, JACM 2021) with fixed-sequence testing over a threshold grid. Pick the
most permissive abstention threshold whose risk is still provably bounded:

    P( R(threshold) <= alpha ) >= 1 - delta

where the outer probability is over the calibration draw. In words: with 95% confidence,
the deployed abstention policy answers-when-it-shouldn't at most 5% of the time. That is a
statement an operator can hold the system to, which "we set it to 0.35" is not.

Both are validated in `test_conformal.py` the only way that means anything — by simulating
the calibrate-then-deploy cycle many times and measuring how often the guarantee is broken.

Standard library only, deterministic.
"""
from __future__ import annotations

import math

__all__ = [
    "conformal_quantile",
    "AdaptiveSetSizer",
    "SetSizerCalibration",
    "binomial_cdf",
    "hoeffding_bentkus_p",
    "risk_controlling_threshold",
    "RiskCertificate",
]


# --------------------------------------------------------------------------------------
# split conformal prediction — coverage
# --------------------------------------------------------------------------------------

def conformal_quantile(scores: list[float], alpha: float) -> float:
    """The (n+1) finite-sample corrected empirical quantile.

    The correction is the whole trick. Taking the plain (1-alpha) quantile of n calibration
    scores under-covers, because the test point is an (n+1)-th draw that was not there when
    the quantile was computed. Using rank ceil((n+1)(1-alpha)) instead makes the coverage
    guarantee exact rather than asymptotic — it holds at n = 20 as surely as at n = 20,000.

    Returns +inf when n is too small for the requested alpha, which is the honest answer:
    with 10 calibration points you cannot promise 99% coverage, and the caller should get
    a set containing everything rather than a false guarantee.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    n = len(scores)
    if n == 0:
        return math.inf
    rank = math.ceil((n + 1) * (1.0 - alpha))
    if rank > n:
        return math.inf                       # sample too small to certify this alpha
    return sorted(scores)[rank - 1]


def minimum_calibration_size(alpha: float) -> int:
    """Smallest calibration set that can certify `alpha` at all: ceil(1/alpha) - 1."""
    return math.ceil(1.0 / alpha) - 1


class SetSizerCalibration:
    """A fitted adaptive-K rule and the evidence behind it."""

    __slots__ = ("alpha", "threshold", "n_calibration", "temperature",
                 "mean_k_on_calibration", "coverage_on_calibration", "cap_binds")

    def __init__(self, alpha: float, threshold: float, n_calibration: int,
                 temperature: float, mean_k: float, coverage: float,
                 cap_binds: float = 0.0):
        self.alpha = alpha
        self.threshold = threshold
        self.n_calibration = n_calibration
        self.temperature = temperature
        self.mean_k_on_calibration = mean_k
        self.coverage_on_calibration = coverage
        # Share of calibration queries where max_k truncated the certified set. Non-zero
        # means the cap, not the calibration, is deciding — and the coverage guarantee is
        # only as good as the cap allows.
        self.cap_binds = cap_binds

    @property
    def certified(self) -> bool:
        """True when the calibration set was large enough to certify this alpha at all."""
        return math.isfinite(self.threshold)

    @property
    def guarantee_intact(self) -> bool:
        """The coverage promise survives only while the hard cap never truncates a set."""
        return self.certified and self.cap_binds == 0.0

    def as_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "threshold": self.threshold if self.certified else None,
            "certified": self.certified,
            "guarantee_intact": self.guarantee_intact,
            "cap_binds": self.cap_binds,
            "n_calibration": self.n_calibration,
            "temperature": self.temperature,
            "mean_k_on_calibration": self.mean_k_on_calibration,
            "coverage_on_calibration": self.coverage_on_calibration,
            "minimum_calibration_size": minimum_calibration_size(self.alpha),
        }

    def __repr__(self) -> str:
        state = f"threshold={self.threshold:.4f}" if self.certified else "UNCERTIFIED"
        return (f"SetSizerCalibration(alpha={self.alpha}, {state}, "
                f"n={self.n_calibration}, mean_k={self.mean_k_on_calibration:.2f})")


class AdaptiveSetSizer:
    """Choose how many passages to send to the LLM, per query, with a coverage guarantee.

    Reranker scores are turned into a distribution over candidates and accumulated in rank
    order; the set is the shortest prefix whose mass reaches a calibrated level. Where the
    top passage dominates, that prefix is one or two passages. Where the scores are flat —
    which is exactly when retrieval is uncertain and truncating is dangerous — it is many.
    A fixed top-K gets this backwards in both directions at once: too much context for easy
    queries, too little for hard ones.

    The nonconformity score is the cumulative mass required to reach the first relevant
    document. Calibrating its quantile is what converts "this heuristic seems reasonable"
    into a coverage statement about the next query.
    """

    def __init__(self, alpha: float = 0.10, temperature: float = 1.0,
                 max_k: int | None = None):
        """`max_k` is a hard context cap, and it overrides the certificate when it binds.

        Truncating a certified set can drop the relevant passage, so a non-zero
        `cap_binds` on the calibration report means the guarantee is bounded by the cap
        rather than by alpha. Left as None the guarantee is unconditional.
        """
        if not 0.0 < alpha < 1.0:
            raise ValueError(f"alpha must be in (0, 1), got {alpha}")
        self.alpha = alpha
        self.temperature = temperature
        self.max_k = max_k
        self.calibration: SetSizerCalibration | None = None

    # -- scoring ------------------------------------------------------------------

    def _distribution(self, scores: list[float]) -> list[float]:
        """Softmax over reranker scores, in the order they were ranked.

        Softmax rather than a plain normalisation because reranker logits are unbounded and
        can be negative; the temperature is the one knob that trades average set size
        against how sharply the model's own confidence is trusted.
        """
        if not scores:
            return []
        top = max(scores)
        weights = [math.exp((s - top) / self.temperature) for s in scores]
        total = sum(weights)
        return [w / total for w in weights] if total > 0 else [1.0 / len(scores)] * len(scores)

    def nonconformity(self, scores: list[float], relevant_index: int) -> float:
        """Mass the reranker put on wrong passages before reaching a right one.

        Zero when the relevant passage is ranked first, and large when the reranker either
        ranked it low or spread its confidence thinly — which is precisely the situation
        the calibrated set has to be wide enough to survive.

        The prefix is *exclusive* of the relevant passage, and that has to match the
        inclusion rule in `size()` exactly: the set keeps every passage whose exclusive
        prefix mass is <= the calibrated quantile, so coverage holds if and only if the
        relevant passage's own score is <= that quantile. Score inclusively against an
        exclusive rule and the guarantee still holds but the sets are needlessly wide;
        get the inequality backwards and it does not hold at all.
        """
        if not 0 <= relevant_index < len(scores):
            raise ValueError(f"relevant_index {relevant_index} outside 0..{len(scores) - 1}")
        return sum(self._distribution(scores)[:relevant_index])

    # -- fitting ------------------------------------------------------------------

    def calibrate(self, examples: list[tuple[list[float], int]]) -> SetSizerCalibration:
        """Fit on (ranked scores, index of the first relevant document) pairs.

        Queries with no relevant document in the candidate list are excluded rather than
        scored as 1.0: they are a recall failure upstream, and folding them in here would
        silently inflate every set to full length to paper over it.
        """
        usable = [(s, i) for s, i in examples if s and 0 <= i < len(s)]
        scores = [self.nonconformity(s, i) for s, i in usable]
        threshold = conformal_quantile(scores, self.alpha)
        self.calibration = SetSizerCalibration(self.alpha, threshold, len(scores),
                                               self.temperature, 0.0, 0.0)
        if not usable:
            return self.calibration

        # Re-measure on the calibration set itself. These are in-sample numbers and are
        # reported as such — the guarantee is about unseen queries, and the reason to look
        # at them is to see what the certified sets actually cost and whether the cap bit.
        uncapped = AdaptiveSetSizer(self.alpha, self.temperature, max_k=None)
        uncapped.calibration = self.calibration
        sizes = [self.size(s) for s, _ in usable]
        self.calibration.mean_k_on_calibration = sum(sizes) / len(sizes)
        self.calibration.coverage_on_calibration = (
            sum(1 for (s, i), k in zip(usable, sizes) if i < k) / len(usable))
        self.calibration.cap_binds = (
            sum(1 for (s, _), k in zip(usable, sizes) if k < uncapped.size(s)) / len(usable))
        return self.calibration

    def load(self, threshold: float, alpha: float | None = None,
             temperature: float | None = None) -> "AdaptiveSetSizer":
        """Restore a calibration computed elsewhere — the serving path's entry point."""
        self.alpha = alpha if alpha is not None else self.alpha
        self.temperature = temperature if temperature is not None else self.temperature
        self.calibration = SetSizerCalibration(self.alpha, threshold, 0, self.temperature,
                                               0.0, 0.0)
        return self

    # -- serving ------------------------------------------------------------------

    def size(self, scores: list[float]) -> int:
        """Passages to keep for this query — at least one, at most `max_k`.

        Keeps every passage whose *exclusive* prefix mass is at or below the calibrated
        quantile. The first passage always qualifies (its prefix mass is zero), so a query
        never comes back with an empty context.
        """
        if not scores:
            return 0
        if self.calibration is None:
            raise RuntimeError("calibrate() or load() before sizing")
        ceiling = min(self.max_k, len(scores)) if self.max_k else len(scores)
        if not self.calibration.certified:
            return ceiling                     # no certificate: fall back to everything
        threshold = self.calibration.threshold
        kept = 0
        prefix = 0.0
        for mass in self._distribution(scores):
            if prefix > threshold:
                break
            kept += 1
            prefix += mass
        return min(kept, ceiling)


# --------------------------------------------------------------------------------------
# risk-controlling thresholds — abstention
# --------------------------------------------------------------------------------------

def binomial_cdf(k: int, n: int, p: float) -> float:
    """P(Bin(n, p) <= k), summed in log space so large n does not overflow."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(k + 1):
        log_term = (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                    + i * log_p + (n - i) * log_q)
        total += math.exp(log_term)
    return min(1.0, total)


def _kl(a: float, b: float) -> float:
    """KL divergence between Bernoulli(a) and Bernoulli(b), used by the Hoeffding bound."""
    if a <= 0.0:
        return -math.log1p(-b)
    if a >= 1.0:
        return -math.log(b)
    return a * math.log(a / b) + (1.0 - a) * math.log((1.0 - a) / (1.0 - b))


def hoeffding_bentkus_p(empirical_risk: float, n: int, alpha: float) -> float:
    """p-value for H0: true risk > alpha, given the risk observed on n calibration points.

    The minimum of two bounds (Bates et al., JACM 2021). Hoeffding's is sharp when the
    empirical risk sits well below alpha; Bentkus' binomial bound is sharp in the tail
    where Hoeffding's is loose. Taking the smaller is valid and strictly tighter than
    either — which matters, because a loose bound spends the error budget on nothing and
    leaves a needlessly conservative threshold in production.
    """
    if n <= 0:
        return 1.0
    if empirical_risk >= alpha:
        return 1.0                                   # no evidence the risk is controlled
    hoeffding = math.exp(-n * _kl(empirical_risk, alpha))
    bentkus = math.e * binomial_cdf(math.ceil(n * empirical_risk), n, alpha)
    return min(1.0, hoeffding, bentkus)


def queries_for_risk_bound(observed_risk: float, alpha: float, delta: float) -> int | None:
    """Calibration queries needed to certify `observed_risk` against `alpha` at `delta`.

    Inverts the Hoeffding half of the bound: n >= ln(1/delta) / KL(risk || alpha). The
    answer is usually the only actionable thing about a failed certification. A policy whose
    true risk is 2% will not certify against a 5% target on 150 queries — not because the
    policy is unsafe, but because the evidence is thin, and the fix is more calibration data
    rather than a looser target. Returns None when the observed risk already meets or
    exceeds alpha, where no sample size helps.
    """
    if observed_risk >= alpha or observed_risk < 0.0:
        return None
    divergence = _kl(observed_risk, alpha)
    if divergence <= 0.0:
        return None
    return math.ceil(math.log(1.0 / delta) / divergence)


class RiskCertificate:
    """The chosen threshold and the guarantee it carries."""

    __slots__ = ("threshold", "alpha", "delta", "n_calibration", "empirical_risk",
                 "p_value", "certified", "candidates_tested", "empirical_abstention",
                 "blocked_by")

    def __init__(self, threshold: float | None, alpha: float, delta: float,
                 n_calibration: int, empirical_risk: float, p_value: float,
                 candidates_tested: int, empirical_abstention: float,
                 blocked_by: dict | None = None):
        self.threshold = threshold
        self.alpha = alpha
        self.delta = delta
        self.n_calibration = n_calibration
        self.empirical_risk = empirical_risk
        self.p_value = p_value
        self.certified = threshold is not None
        self.candidates_tested = candidates_tested
        self.empirical_abstention = empirical_abstention
        # The candidate that ended the fixed sequence, and what it would take to get past
        # it. Without this a stalled walk looks like a safe answer instead of a thin one.
        self.blocked_by = blocked_by

    @property
    def degenerate(self) -> bool:
        """Certified by never answering — a valid guarantee attached to a useless system."""
        return self.certified and self.empirical_abstention >= 0.999

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold, "certified": self.certified,
            "degenerate": self.degenerate,
            "alpha": self.alpha, "delta": self.delta,
            "n_calibration": self.n_calibration,
            "empirical_risk": self.empirical_risk,
            "empirical_abstention": self.empirical_abstention,
            "p_value": self.p_value, "candidates_tested": self.candidates_tested,
            "blocked_by": self.blocked_by,
        }

    def statement(self) -> str:
        if not self.certified:
            return (f"No threshold on the grid controls risk at alpha={self.alpha} with "
                    f"confidence {1 - self.delta:.0%} on {self.n_calibration} calibration "
                    f"queries — the system abstains on everything until it can.")
        return (f"With probability at least {1 - self.delta:.0%} over the calibration draw, "
                f"a threshold of {self.threshold:.4f} holds the risk at or below "
                f"{self.alpha:.0%} on unseen queries "
                f"(observed {self.empirical_risk:.1%} on {self.n_calibration} queries, "
                f"p = {self.p_value:.4f}).")

    def __repr__(self) -> str:
        state = f"{self.threshold:.4f}" if self.certified else "UNCERTIFIED"
        return (f"RiskCertificate({state}, alpha={self.alpha}, delta={self.delta}, "
                f"risk={self.empirical_risk:.3f})")


def risk_controlling_threshold(candidates: list[float], loss_fn, alpha: float = 0.05,
                               delta: float = 0.05,
                               abstention_fn=None) -> RiskCertificate:
    """Most permissive threshold whose risk is provably at or below `alpha`.

    `candidates` must be ordered **most conservative first** — for an abstention gate that
    means highest threshold (abstain most) to lowest. Walking a fixed sequence and stopping
    at the first failure controls the family-wise error rate with no multiplicity
    correction at all: under the null the walk terminates before reaching an uncontrolled
    threshold, so testing a hundred candidates costs exactly as much error budget as
    testing one. Correcting for the grid instead — as a naive implementation would — throws
    away most of the budget and returns a threshold far more conservative than necessary.

    `loss_fn(threshold) -> float` is the empirical risk on the calibration set: the share of
    calibration queries where deploying this threshold produces the outcome being bounded
    (answering from insufficient context, say). `abstention_fn` is optional and only
    reported — the cost side of the trade, so the certificate shows what the guarantee
    bought and what it cost.
    """
    if not 0.0 < alpha < 1.0 or not 0.0 < delta < 1.0:
        raise ValueError("alpha and delta must both be in (0, 1)")

    best: RiskCertificate | None = None
    blocked: dict | None = None
    for tested, threshold in enumerate(candidates, start=1):
        risk, n = loss_fn(threshold)
        p = hoeffding_bentkus_p(risk, n, alpha)
        if p > delta:                                # first failure ends the fixed sequence
            blocked = {
                "threshold": threshold,
                "empirical_risk": risk,
                "p_value": p,
                "abstention": abstention_fn(threshold) if abstention_fn else None,
                "queries_needed": queries_for_risk_bound(risk, alpha, delta),
                "n_calibration": n,
            }
            break
        best = RiskCertificate(
            threshold, alpha, delta, n, risk, p, tested,
            abstention_fn(threshold) if abstention_fn else float("nan"))

    if best is None:
        risk, n = loss_fn(candidates[0]) if candidates else (1.0, 0)
        return RiskCertificate(None, alpha, delta, n, risk,
                               hoeffding_bentkus_p(risk, n, alpha), 1,
                               abstention_fn(candidates[0]) if abstention_fn and candidates
                               else float("nan"), blocked)
    best.blocked_by = blocked
    return best
