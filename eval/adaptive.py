"""Keeping a conformal guarantee alive in a system that keeps changing (docs/adr/0016).

[ADR 0013](../docs/adr/0013-conformal-risk-control.md) calibrates a coverage guarantee and
writes the threshold into `application.yml`. Every word of that guarantee rests on
**exchangeability**: the queries it was calibrated on and the queries it will serve must be
draws from the same distribution.

A running system violates that on its second day. Documents are ingested, so the corpus the
reranker scores against is not the corpus it was calibrated on. Query mix moves. Models get
swapped. A certificate calibrated in January is not valid in June, and — the part that
matches every other failure this repository has chased — **nothing notices**. The threshold
is still there, the metrics still render, and the coverage it promises has quietly stopped
being true.

**Adaptive conformal inference** (Gibbs & Candès, *Adaptive Conformal Inference Under
Distribution Shift*, NeurIPS 2021) turns the miscoverage rate into a feedback signal:

    alpha_{t+1} = alpha_t + gamma * (alpha - err_t)

where `err_t` is 1 when the set at step `t` missed. A miss *lowers* `alpha_t`, and a lower
level is a **wider** set — the quantile is taken at rank `ceil((n+1)(1-alpha))`, so less
alpha keeps more of the ranking. Miss too often and the set grows; miss too rarely and it
shrinks. The result is not a heuristic — summing the update telescopes to an exact
identity,

    (1/T) sum(err_t) - alpha = (alpha_1 - alpha_{T+1}) / (T * gamma)

and `alpha_t` is trapped in `[-gamma, 1 + gamma]` because an empty set always misses and a
full set never does. So the realised long-run miscoverage converges to `alpha` at rate
`O(1/T)` **with no distributional assumption at all** — not exchangeability, not
stationarity, not even that the shift is random rather than adversarial. What it gives up
is the finite-sample guarantee at any single step; what it buys is a guarantee that
survives the corpus changing underneath it.

Worth being exact about the division of labour, because measuring it corrected what this
module originally claimed. A **rolling window** already absorbs a pure location shift on its
own: refill the calibration set with recent scores and the quantile moves with the data, so
`alpha_t` comes straight back to target and the level controller has nothing to do. What the
controller adds is the case the window cannot track — a change of *shape*, a mass of ties, an
adversary — where it holds a corrective offset and the long-run miscoverage still converges.
The negative control in the tests confirms the stakes are real either way: a threshold
calibrated once and frozen loses coverage on exactly the data both of these survive.

`gamma` is the usual awkward constant: too small and the controller cannot keep up with a
shift, too large and it thrashes on noise. `DtACI` (Gibbs & Candès, JMLR 2024) removes it
by running several candidates as experts and aggregating them by their own realised loss,
which is possible here because the nonconformity score makes every expert's hypothetical
outcome computable after the fact.

Standard library only, deterministic.
"""
from __future__ import annotations

import math
from collections import deque

from conformal import conformal_quantile

__all__ = ["AdaptiveConformal", "DtACI", "coverage_bound", "DEFAULT_GAMMA"]

DEFAULT_ALPHA = 0.10
DEFAULT_GAMMA = 0.05
DEFAULT_WINDOW = 500
# Candidate step sizes for DtACI: an order of magnitude either side of the default, since
# the right one depends on how fast the corpus moves and that is not knowable in advance.
GAMMA_CANDIDATES = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2)


def coverage_bound(alpha_1: float, gamma: float, steps: int) -> float:
    """How far realised miscoverage can be from the target after `steps`.

    From the telescoped update, the gap is `(alpha_1 - alpha_{T+1}) / (T * gamma)`, and
    `alpha_t` never leaves `[-gamma, 1 + gamma]`. This is a deterministic bound — it holds
    for every sequence, including one chosen by an adversary who knows the controller.
    """
    if steps <= 0 or gamma <= 0:
        return float("inf")
    return (max(alpha_1, 1.0 - alpha_1) + 1.0 + 2.0 * gamma) / (steps * gamma)


class AdaptiveConformal:
    """A conformal threshold that tracks the target coverage as the data moves under it.

    Holds a rolling window of recent nonconformity scores — the calibration set, refreshed
    — and a level `alpha_t` driven by the observed miscoverage. `threshold()` is what the
    serving path applies; `observe()` is what closes the loop.
    """

    def __init__(self, alpha: float = DEFAULT_ALPHA, gamma: float = DEFAULT_GAMMA,
                 window: int = DEFAULT_WINDOW):
        if not 0.0 < alpha < 1.0:
            raise ValueError(f"alpha must be in (0, 1), got {alpha}")
        if gamma <= 0.0:
            raise ValueError(f"gamma must be positive, got {gamma}")
        self.alpha = alpha
        self.gamma = gamma
        self.alpha_t = alpha
        self.scores: deque[float] = deque(maxlen=window)
        self.steps = 0
        self.misses = 0
        self._alpha_history: list[float] = []

    # -- the level, and the threshold it implies ------------------------------------

    @property
    def effective_alpha(self) -> float:
        """`alpha_t` clipped to a level a quantile can be taken at."""
        return min(1.0, max(0.0, self.alpha_t))

    def threshold(self) -> float:
        """The nonconformity cutoff to apply right now.

        Below zero the controller is asking for a set that cannot miss, so everything is
        kept; at or above one it is asking for the empty set. Both are legitimate states of
        the recursion and both are self-correcting: a full set never misses and pushes the
        level back down, an empty set always misses and pushes it back up.
        """
        if self.alpha_t <= 0.0:
            return math.inf
        if self.alpha_t >= 1.0:
            return -math.inf
        if not self.scores:
            return math.inf
        return conformal_quantile(list(self.scores), self.effective_alpha)

    def covers(self, score: float) -> bool:
        """Whether an item with this nonconformity score falls inside the current set."""
        return score <= self.threshold()

    # -- closing the loop ------------------------------------------------------------

    def observe(self, score: float, covered: bool | None = None) -> bool:
        """Record one outcome and advance the level.

        `covered` is the ground truth for this step. Left as None it is derived from the
        score against the current threshold, which is the right thing when the score is
        itself the observation — and wrong when coverage is measured by something else, so
        the caller can override it.
        """
        if covered is None:
            covered = self.covers(score)
        self.scores.append(score)
        self.steps += 1
        self.misses += 0 if covered else 1
        self._alpha_history.append(self.alpha_t)
        self.alpha_t = self.alpha_t + self.gamma * (self.alpha - (0.0 if covered else 1.0))
        return covered

    # -- what it is doing -------------------------------------------------------------

    @property
    def realized_miscoverage(self) -> float:
        return self.misses / self.steps if self.steps else 0.0

    @property
    def within_bound(self) -> bool:
        """The deterministic guarantee, checked rather than asserted."""
        gap = abs(self.realized_miscoverage - self.alpha)
        return gap <= coverage_bound(self.alpha, self.gamma, self.steps) + 1e-12

    @property
    def compensating(self) -> bool:
        """True when the level is holding a large standing offset to keep coverage.

        Named carefully, because the obvious name is wrong. This is *not* a drift detector:
        a pure location shift leaves it False, since the rolling window tracks the shift by
        itself and the level returns to target with nothing to compensate for. It goes True
        when the window quantile is systematically off and only a displaced level is holding
        coverage together — a change of shape, a mass of tied scores, an adversarial stream.
        That is the more useful signal of the two, and it is the one a frozen threshold can
        never produce.

        Under exchangeability `alpha_t` wanders around `alpha`; measured over 2,000 steps at
        gamma=0.05 the widest stationary excursion was 2.6 gamma, so 4 gamma sits above the
        noise with room to spare (0/60 false positives, see the tests).
        """
        return abs(self.alpha_t - self.alpha) > 4 * self.gamma

    def report(self) -> dict:
        return {
            "alpha_target": self.alpha, "alpha_now": self.alpha_t, "gamma": self.gamma,
            "steps": self.steps, "misses": self.misses,
            "realized_miscoverage": self.realized_miscoverage,
            "bound": coverage_bound(self.alpha, self.gamma, self.steps),
            "within_bound": self.within_bound, "compensating": self.compensating,
            "window": self.scores.maxlen, "calibration_points": len(self.scores),
        }


class DtACI:
    """Adaptive conformal inference without having to pick gamma.

    Runs one controller per candidate step size and aggregates their levels by exponential
    weighting on the pinball loss each would have incurred. The trick that makes this
    possible is that after the fact every expert's outcome is computable: coverage at level
    `a` is just whether the observed score exceeded the quantile at `a`, so no expert has to
    be run counterfactually against the world.

    Gibbs & Candès, *Conformal Inference for Online Prediction with Arbitrary Distribution
    Shifts*, JMLR 2024.
    """

    def __init__(self, alpha: float = DEFAULT_ALPHA,
                 gammas: tuple[float, ...] = GAMMA_CANDIDATES,
                 window: int = DEFAULT_WINDOW, eta: float = 2.0):
        if not gammas:
            raise ValueError("DtACI needs at least one candidate step size")
        self.alpha = alpha
        self.eta = eta
        self.experts = [AdaptiveConformal(alpha, g, window) for g in gammas]
        self.gammas = tuple(gammas)
        self.losses = [0.0] * len(self.experts)
        self.scores: deque[float] = deque(maxlen=window)
        self.steps = 0
        self.misses = 0

    @property
    def weights(self) -> list[float]:
        """Exponential weights, shifted by the minimum loss so the exponent cannot overflow."""
        best = min(self.losses)
        raw = [math.exp(-self.eta * (loss - best)) for loss in self.losses]
        total = sum(raw)
        return [w / total for w in raw] if total > 0 else [1 / len(raw)] * len(raw)

    @property
    def alpha_t(self) -> float:
        return sum(w * e.alpha_t for w, e in zip(self.weights, self.experts))

    def threshold(self) -> float:
        level = min(1.0, max(0.0, self.alpha_t))
        if level <= 0.0 or not self.scores:
            return math.inf
        if level >= 1.0:
            return -math.inf
        return conformal_quantile(list(self.scores), level)

    def covers(self, score: float) -> bool:
        return score <= self.threshold()

    def observe(self, score: float, covered: bool | None = None) -> bool:
        if covered is None:
            covered = self.covers(score)
        # Pinball loss of each expert's level against what actually happened: an expert that
        # asked for a tight set is charged when the item was missed, and charged less when
        # it was covered. This is what separates a well-tuned gamma from a lucky one.
        miss = 0.0 if covered else 1.0
        for index, expert in enumerate(self.experts):
            level = expert.effective_alpha
            self.losses[index] += (self.alpha - miss) * level + max(0.0, miss - level)
            expert.observe(score, covered)

        self.scores.append(score)
        self.steps += 1
        self.misses += 0 if covered else 1
        return covered

    @property
    def realized_miscoverage(self) -> float:
        return self.misses / self.steps if self.steps else 0.0

    @property
    def best_gamma(self) -> float:
        """The step size currently carrying the most weight."""
        weights = self.weights
        return self.gammas[weights.index(max(weights))]

    def report(self) -> dict:
        return {
            "alpha_target": self.alpha, "alpha_now": self.alpha_t,
            "steps": self.steps, "misses": self.misses,
            "realized_miscoverage": self.realized_miscoverage,
            "gammas": list(self.gammas), "weights": self.weights,
            "best_gamma": self.best_gamma,
        }
