"""Anytime-valid evaluation: stop when the verdict is decided (docs/adr/0012).

Retrieval evaluation is sequential — queries are scored one at a time — but it is
analysed as if it were a single batch, and that mismatch costs twice.

**It invalidates the statistics.** A 95% interval is a promise about one look at a
finished sample. Watching a CI log and forming a view before it ends is *peeking*, and
under peeking the guarantee collapses: an interval recomputed after every query excludes
the true mean far more than 5% of the time. Everyone peeks. The guarantee was never
protecting the way the tool is actually used.

**It wastes the budget.** A gate asking "is MRR@10 above 0.85?" against a system scoring
0.95 is settled long before the last query, yet the harness runs every one. With an LLM
judge in the loop each query is money and seconds, and most of them buy no information.

Both problems have one fix. A *confidence sequence* is an interval valid at **every**
sample size simultaneously, so it may be inspected continuously, and a decision may be
taken the moment it is determined — which is also what makes early stopping legitimate
rather than a form of p-hacking.

The construction here is the hedged capital process of **Waudby-Smith & Ramdas, "Estimating
means of bounded random variables by betting" (JRSS-B, 2024)**. It suits retrieval metrics
unusually well: every one of them — reciprocal rank, nDCG, recall, a judge's groundedness
score — lives in [0, 1], which is exactly the regime where betting confidence sequences are
tightest, and it assumes nothing about the shape of the distribution. Reciprocal rank is
discrete, skewed and bounded, so a normal approximation is a poor fit at any sample size.

How it works, in one paragraph. To test whether the mean is `m`, bet repeatedly on each
next observation at odds implied by `m`. Wealth starts at 1. If `m` is the true mean the
bets are fair, so wealth is a non-negative martingale and by Ville's inequality it exceeds
`1/alpha` at *any* point with probability at most `alpha`. Wealth above `1/alpha` is
therefore evidence against `m`, valid whenever it is looked at. The confidence sequence is
every `m` not yet rejected; the gate is the single `m` at the threshold.

An important caveat, stated rather than buried: this targets the **superpopulation** mean —
the score the system would obtain on the query distribution the gold set is drawn from,
not the exact average of these particular queries. That is the quantity worth gating on,
but it requires the evaluation order to be a random shuffle rather than the file order, so
`shuffled()` is part of the interface and the seed is recorded with the verdict.

Standard library only, seeded, and validated in `test_sequential.py` against the property
that matters: coverage under continuous inspection, measured side by side with the fixed-N
interval it replaces.
"""
from __future__ import annotations

import math
import random

__all__ = [
    "CapitalProcess",
    "ConfidenceSequence",
    "SequentialGate",
    "GateVerdict",
    "paired_gate",
    "shuffled",
    "DEFAULT_ALPHA",
]

DEFAULT_ALPHA = 0.05
# Bet at most half the allowed stake. Keeps wealth strictly positive (so the log-space
# update never sees a non-positive factor) and costs little tightness. WSR section 3.
BET_FRACTION = 0.5
# Two-sided hedge: half the error budget bets on "the mean is higher", half on "lower".
HEDGE = 0.5
# Resolution of the grid a reported interval is read off. 0.005 is finer than any
# retrieval metric is meaningfully quoted to.
DEFAULT_GRID = 200


def shuffled(items: list, seed: int) -> list:
    """A seeded shuffle — sequential validity needs random order, reproducibility needs a seed.

    The gold set arrives grouped: all the Korean queries together, all the Kafka ones
    together. Scored in file order the early sample is not representative of the whole,
    and a sequential procedure would be deciding about a different population than the
    one it reports on.
    """
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


class CapitalProcess:
    """Wealth from betting against a single hypothesised mean `m`, updated in log space.

    Two books are kept: one that profits when observations run above `m`, one below. Under
    the null both are non-negative martingales starting at 1, so Ville's inequality bounds
    the chance that either ever reaches its rejection level — at any stopping time, chosen
    however the observer likes.
    """

    __slots__ = ("m", "alpha", "n", "_sum", "_log_up", "_log_down", "_var", "_running_mean",
                 "_sq_dev", "_log_reject", "_c")

    def __init__(self, m: float, alpha: float = DEFAULT_ALPHA, bet_fraction: float = BET_FRACTION):
        if not 0.0 < m < 1.0:
            raise ValueError(f"hypothesised mean must be in (0, 1), got {m}")
        self.m = m
        self.alpha = alpha
        self._c = bet_fraction
        self.n = 0
        self._sum = 0.0
        self._log_up = 0.0          # log wealth of the book betting the mean is above m
        self._log_down = 0.0        # ... and below
        # Running mean and variance estimates seeded with a uniform prior (1/2, 1/4), which
        # is what makes the first few bets cautious instead of wild.
        self._running_mean = 0.5
        self._sq_dev = 0.0
        self._var = 0.25
        self._log_reject = math.log(1.0 / (alpha * HEDGE))

    def update(self, x: float) -> "CapitalProcess":
        """Place the next pair of bets. `x` must lie in [0, 1]."""
        if not 0.0 <= x <= 1.0:
            raise ValueError(f"observations must lie in [0, 1], got {x}")
        t = self.n + 1
        # The stake is predictable: it uses only what was known before x arrived, which is
        # the condition the martingale argument rests on.
        stake = math.sqrt(2.0 * math.log(2.0 / self.alpha)
                          / (self._var * t * math.log(1.0 + t)))
        edge = x - self.m
        up = min(stake, self._c / self.m)
        down = min(stake, self._c / (1.0 - self.m))
        self._log_up += math.log1p(up * edge)
        self._log_down += math.log1p(-down * edge)

        # Advance the estimates the *next* bet will use. The squared deviation is taken
        # against the mean as it stood before x arrived, keeping both quantities functions
        # of the past only.
        self.n = t
        self._sum += x
        self._sq_dev += (x - self._running_mean) ** 2
        self._running_mean = (0.5 + self._sum) / (1.0 + t)
        self._var = (0.25 + self._sq_dev) / (1.0 + t)
        return self

    @property
    def mean(self) -> float:
        return self._sum / self.n if self.n else 0.0

    @property
    def log_wealth(self) -> float:
        return max(self._log_up, self._log_down)

    @property
    def rejected(self) -> bool:
        """True once the evidence against `m` has crossed the level Ville's bound protects."""
        return self.log_wealth >= self._log_reject

    @property
    def direction(self) -> int:
        """+1 if the evidence says the mean exceeds `m`, -1 if below, 0 while undecided."""
        if not self.rejected:
            return 0
        return 1 if self._log_up >= self._log_down else -1


class ConfidenceSequence:
    """An interval valid at every sample size at once — safe to watch, safe to stop on.

    Maintains one capital process per grid point and reports the range of means not yet
    rejected. Cost is O(grid) per observation, which at the default resolution is a few
    hundred multiplications per query — irrelevant next to an HTTP round trip.
    """

    def __init__(self, alpha: float = DEFAULT_ALPHA, grid: int = DEFAULT_GRID):
        # Grid points sit at cell midpoints, so 0 and 1 are never hypothesised and the
        # stake bounds c/m and c/(1-m) stay finite.
        self._points = [(j + 0.5) / grid for j in range(grid)]
        self._books = [CapitalProcess(m, alpha) for m in self._points]
        self.alpha = alpha
        self.n = 0
        self._sum = 0.0

    def update(self, x: float) -> tuple[float, float]:
        for book in self._books:
            book.update(x)
        self.n += 1
        self._sum += x
        return self.bounds

    def extend(self, values) -> tuple[float, float]:
        for x in values:
            self.update(x)
        return self.bounds

    @property
    def mean(self) -> float:
        return self._sum / self.n if self.n else 0.0

    @property
    def bounds(self) -> tuple[float, float]:
        alive = [m for m, book in zip(self._points, self._books) if not book.rejected]
        if not alive:                      # every hypothesis rejected — degenerate, widen out
            return (0.0, 1.0)
        return (min(alive), max(alive))

    def contains(self, m: float) -> bool:
        lo, hi = self.bounds
        return lo <= m <= hi


class GateVerdict:
    """The outcome of a sequential gate, including what it cost to reach."""

    __slots__ = ("metric", "threshold", "decision", "queries_used", "queries_available",
                 "mean", "alpha", "seed")

    def __init__(self, metric: str, threshold: float, decision: str, queries_used: int,
                 queries_available: int, mean: float, alpha: float, seed: int | None):
        self.metric = metric
        self.threshold = threshold
        self.decision = decision          # "pass" | "fail" | "undecided"
        self.queries_used = queries_used
        self.queries_available = queries_available
        self.mean = mean
        self.alpha = alpha
        self.seed = seed

    @property
    def passed(self) -> bool:
        """Undecided is not a pass: a gate that could not conclude has not cleared anything."""
        return self.decision == "pass"

    @property
    def decided(self) -> bool:
        return self.decision != "undecided"

    @property
    def saved(self) -> int:
        return self.queries_available - self.queries_used

    @property
    def saved_fraction(self) -> float:
        return self.saved / self.queries_available if self.queries_available else 0.0

    def as_dict(self) -> dict:
        return {
            "metric": self.metric, "threshold": self.threshold, "decision": self.decision,
            "queries_used": self.queries_used, "queries_available": self.queries_available,
            "saved": self.saved, "saved_fraction": self.saved_fraction,
            "mean_at_stop": self.mean, "alpha": self.alpha, "seed": self.seed,
        }

    def __repr__(self) -> str:
        return (f"GateVerdict({self.metric} vs {self.threshold:.2f}: {self.decision} "
                f"after {self.queries_used}/{self.queries_available})")


class SequentialGate:
    """Decide `mean >= threshold` as early as the evidence allows.

    Only the threshold itself has to be tested: the confidence sequence excludes it exactly
    when the whole interval has moved to one side, so a single capital process answers the
    gate question at O(1) per query. Which book crossed gives the direction.
    """

    def __init__(self, threshold: float, metric: str = "metric",
                 alpha: float = DEFAULT_ALPHA):
        self.threshold = threshold
        self.metric = metric
        self.alpha = alpha
        self._book = CapitalProcess(threshold, alpha)

    def update(self, x: float) -> str:
        self._book.update(x)
        return self.decision

    @property
    def decision(self) -> str:
        direction = self._book.direction
        if direction > 0:
            return "pass"
        if direction < 0:
            return "fail"
        return "undecided"

    @property
    def decided(self) -> bool:
        return self._book.direction != 0

    def run(self, values, seed: int | None = None) -> GateVerdict:
        """Feed values until decided, then stop. Shuffle first unless already randomised."""
        ordered = shuffled(list(values), seed) if seed is not None else list(values)
        for i, x in enumerate(ordered, start=1):
            if self.update(x) != "undecided":
                return GateVerdict(self.metric, self.threshold, self.decision, i,
                                   len(ordered), self._book.mean, self.alpha, seed)
        return GateVerdict(self.metric, self.threshold, "undecided", len(ordered),
                           len(ordered), self._book.mean, self.alpha, seed)


def paired_gate(candidate, baseline, alpha: float = DEFAULT_ALPHA,
                seed: int | None = None, metric: str = "paired") -> GateVerdict:
    """Sequentially decide whether `candidate` beats `baseline` on the same queries.

    Per-query differences live in [-1, 1]; rescaling to [0, 1] puts them back in the regime
    the betting construction covers, and "no difference" becomes the hypothesis that the
    rescaled mean is 1/2. Pairing is what makes this cheap — the query-to-query variance
    that dominates an unpaired comparison cancels.
    """
    candidate, baseline = list(candidate), list(baseline)
    if len(candidate) != len(baseline):
        raise ValueError(f"paired gate needs equal lengths, got "
                         f"{len(candidate)} and {len(baseline)}")
    rescaled = [(a - b + 1.0) / 2.0 for a, b in zip(candidate, baseline)]
    verdict = SequentialGate(0.5, metric, alpha).run(rescaled, seed)
    # Report the mean on the original scale: 2 * rescaled - 1 is the mean difference.
    verdict.mean = 2.0 * verdict.mean - 1.0
    verdict.threshold = 0.0
    return verdict
