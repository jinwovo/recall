"""Claim-level conformal factuality — a guarantee on what the answer says (docs/adr/0017).

ADR 0013 certified the two decisions *before* generation: how many passages to send, and
whether to answer at all. Nothing certified the answer. The groundedness judge grades it
as one unit, so an answer with five supported sentences and one invented one is either
passed whole or thrown away whole, and the reader cannot tell which sentence was the
problem.

This module moves the guarantee onto the claims. Split the answer into atomic claims,
score each one against the retrieved passages (an entailment score — the cross-encoder
already in the stack is one), and drop every claim scoring at or below a calibrated
threshold. Three calibrations, for three different promises:

**Strict — every claim that survives is supported** (Mohri & Hashimoto, ICML 2024).
The nonconformity score of an answer is the highest score any *unsupported* claim in it
received: the threshold you would need to remove all of its false claims. Its corrected
conformal quantile gives

    P( every retained claim of the next answer is supported ) >= 1 - alpha

exactly, at any calibration size, for any scorer — exchangeability is the only assumption.

**Expected share — few false claims, rather than none** (conformal risk control,
Angelopoulos, Bates, Fisch, Lei & Schuster, ICLR 2024). "No false claim at all" is a hard
target, and the 2026 robustness study (Chen et al., arXiv 2603.16817) finds it emptying
answers at high factuality levels. Bounding instead the *expected share* of an answer's
claims that are both false and kept,

    E[ #(unsupported and retained) / #claims ] <= alpha,

keeps far more of the answer. It is a weaker promise and is labelled as one.

**Per group — the promise holds within each domain, not only on average** (Mondrian
conformal; the group-conditional form Conformal-RAG uses, Feng et al., SIGIR 2025). A
marginal 90% can be 98% on easy questions and 70% on hard ones. Calibrating each group
separately makes the guarantee hold inside every group with enough data, and refuses the
groups without.

What none of these can do — and the 2026 study is right to insist — is survive a change in
the score distribution between calibration and deployment. Distractor passages that make
false claims look entailed are exactly such a change. That is the case ADR 0016's coverage
monitor exists for, and `factuality_experiment.py` measures how fast the guarantee goes.

A certificate whose threshold keeps almost nothing is reported as **vacuous** rather than
shipped, the same way ADR 0013 labels a risk certificate that is satisfied by never
answering: a guarantee attached to an empty answer is not a feature.

Standard library only, deterministic.
"""
from __future__ import annotations

import math
from collections import defaultdict

from conformal import conformal_quantile

__all__ = [
    "Claim",
    "nonconformity",
    "retained",
    "calibrate_strict",
    "calibrate_expected_share",
    "calibrate_by_group",
    "FactualityCertificate",
    "evaluate",
]

#: A certificate keeping fewer than this share of supported claims is labelled vacuous.
DEFAULT_MIN_RETENTION = 0.20


class Claim:
    """One atomic claim: the scorer's confidence it is supported, and whether it is."""

    __slots__ = ("score", "supported")

    def __init__(self, score: float, supported: bool):
        if math.isnan(score):
            raise ValueError("a claim score cannot be NaN")
        self.score = float(score)
        self.supported = bool(supported)

    def __repr__(self) -> str:
        return f"Claim({self.score:.3f}, {'ok' if self.supported else 'FALSE'})"


Answer = list  # list[Claim]


def retained(answer: Answer, threshold: float) -> list[Claim]:
    """The claims a threshold keeps. Strictly greater: a claim *at* the threshold is dropped,
    which is what makes the strict guarantee's quantile line up with the filter."""
    return [c for c in answer if c.score > threshold]


def nonconformity(answer: Answer) -> float:
    """The lowest threshold that removes every unsupported claim from this answer.

    -inf for an answer with no false claim: any threshold leaves it factual.
    """
    false_scores = [c.score for c in answer if not c.supported]
    return max(false_scores) if false_scores else -math.inf


def _false_kept_share(answer: Answer, threshold: float) -> float:
    """Loss for the expected-share promise. Normalised by the answer's *total* claims, not
    the retained ones: that keeps it non-increasing in the threshold, which conformal risk
    control needs, and bounded in [0, 1]."""
    if not answer:
        return 0.0
    return sum(1 for c in answer if not c.supported and c.score > threshold) / len(answer)


# --------------------------------------------------------------------------------------
# what a threshold does to a set of answers
# --------------------------------------------------------------------------------------

def evaluate(answers: list[Answer], threshold: float) -> dict:
    """Realised behaviour of a threshold: the guarantee side and the cost side together.

    - ``all_supported``  share of answers whose retained claims are all supported
    - ``false_kept_share``  mean share of each answer's claims that are false and kept
    - ``retention``  share of *supported* claims that survive — what the filter costs
    - ``vacuous_rate``  share of answers that had claims and lost every one of them
    """
    n = len(answers)
    if n == 0:
        return {"n": 0, "all_supported": math.nan, "false_kept_share": math.nan,
                "retention": math.nan, "vacuous_rate": math.nan}
    factual = sum(1 for a in answers if nonconformity(a) <= threshold)
    risk = sum(_false_kept_share(a, threshold) for a in answers) / n
    supported = [c for a in answers for c in a if c.supported]
    kept = sum(1 for c in supported if c.score > threshold)
    nonempty = [a for a in answers if a]
    vacuous = sum(1 for a in nonempty if not retained(a, threshold))
    return {
        "n": n,
        "all_supported": factual / n,
        "false_kept_share": risk,
        "retention": kept / len(supported) if supported else math.nan,
        "vacuous_rate": vacuous / len(nonempty) if nonempty else 0.0,
    }


# --------------------------------------------------------------------------------------
# the certificate
# --------------------------------------------------------------------------------------

class FactualityCertificate:
    """A calibrated claim threshold and the promise it carries."""

    __slots__ = ("threshold", "promise", "alpha", "n_calibration", "calibration",
                 "min_retention", "group")

    PROMISES = ("all-supported", "expected-share")

    def __init__(self, threshold: float, promise: str, alpha: float, n_calibration: int,
                 calibration: dict, min_retention: float = DEFAULT_MIN_RETENTION,
                 group: str | None = None):
        if promise not in self.PROMISES:
            raise ValueError(f"unknown promise {promise!r}")
        self.threshold = threshold
        self.promise = promise
        self.alpha = alpha
        self.n_calibration = n_calibration
        self.calibration = calibration
        self.min_retention = min_retention
        self.group = group

    @property
    def certified(self) -> bool:
        """False when the calibration set is too small to promise anything at this alpha.
        A threshold of -inf is certified: it keeps every claim, and that is the guarantee."""
        return self.threshold != math.inf

    @property
    def vacuous(self) -> bool:
        """Certified, but by deleting the answer. Reported, never shipped as a success."""
        retention = self.calibration.get("retention", math.nan)
        # NaN retention (no supported claim in calibration) cannot show the filter is useful.
        return self.certified and not (retention >= self.min_retention)

    @property
    def usable(self) -> bool:
        return self.certified and not self.vacuous

    def as_dict(self) -> dict:
        return {
            "threshold": (self.threshold if math.isfinite(self.threshold)
                          else None if not self.certified else "-inf"),
            "promise": self.promise, "alpha": self.alpha, "group": self.group,
            "certified": self.certified, "vacuous": self.vacuous,
            "n_calibration": self.n_calibration, "calibration": self.calibration,
        }

    def statement(self) -> str:
        scope = f" in group {self.group!r}" if self.group is not None else ""
        if not self.certified:
            return (f"No claim threshold can be certified{scope} at alpha={self.alpha} from "
                    f"{self.n_calibration} calibration answers; at least "
                    f"{math.ceil(1 / self.alpha) - 1} are needed. Every claim is withheld.")
        what = ("every retained claim is supported with probability at least "
                f"{1 - self.alpha:.0%}" if self.promise == "all-supported" else
                f"the expected share of claims that are false and retained is at most "
                f"{self.alpha:.0%}")
        line = (f"Dropping claims scored <= {self.threshold:.4f}{scope}: {what} "
                f"(n = {self.n_calibration}; keeps {self.calibration['retention']:.0%} of "
                f"supported claims, empties {self.calibration['vacuous_rate']:.0%} of answers).")
        if self.vacuous:
            line += (f" VACUOUS — below the {self.min_retention:.0%} retention floor; more "
                     f"calibration data or a better scorer is the fix, not a looser alpha.")
        return line

    def __repr__(self) -> str:
        state = f"{self.threshold:.4f}" if self.certified else "UNCERTIFIED"
        flag = " vacuous" if self.vacuous else ""
        return f"FactualityCertificate({self.promise}, {state}, alpha={self.alpha}{flag})"


def _check_alpha(alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")


def calibrate_strict(answers: list[Answer], alpha: float = 0.10,
                     min_retention: float = DEFAULT_MIN_RETENTION,
                     group: str | None = None) -> FactualityCertificate:
    """P(every retained claim of a new answer is supported) >= 1 - alpha."""
    _check_alpha(alpha)
    # -inf is a legitimate result — enough calibration answers had no false claim that
    # nothing needs filtering — and +inf is the refusal: too few answers for this alpha.
    tau = conformal_quantile([nonconformity(a) for a in answers], alpha)
    calibration = evaluate(answers, tau)
    return FactualityCertificate(tau, "all-supported", alpha, len(answers), calibration,
                                 min_retention, group)


def calibrate_expected_share(answers: list[Answer], alpha: float = 0.10,
                             min_retention: float = DEFAULT_MIN_RETENTION,
                             group: str | None = None) -> FactualityCertificate:
    """E[share of claims that are false and retained] <= alpha (conformal risk control).

    The most permissive threshold with  (n * R_hat(t) + B) / (n + 1) <= alpha,  B = 1 the
    loss bound. The loss only steps down at claim scores, so those (and -inf) are the only
    candidates worth checking; walking them upward, the first one that satisfies the bound
    is the infimum.
    """
    _check_alpha(alpha)
    n = len(answers)
    if n == 0 or 1.0 / (n + 1) > alpha:
        return FactualityCertificate(math.inf, "expected-share", alpha, n,
                                     {"n": n, "retention": 0.0, "vacuous_rate": 1.0,
                                      "all_supported": math.nan, "false_kept_share": 0.0},
                                     min_retention, group)
    candidates = [-math.inf] + sorted({c.score for a in answers for c in a})
    tau = math.inf
    for t in candidates:
        risk = sum(_false_kept_share(a, t) for a in answers) / n
        if (n * risk + 1.0) / (n + 1) <= alpha:
            tau = t
            break
    return FactualityCertificate(tau, "expected-share", alpha, n, evaluate(answers, tau),
                                 min_retention, group)


def calibrate_by_group(answers: list[Answer], groups: list[str], alpha: float = 0.10,
                       promise: str = "all-supported",
                       min_retention: float = DEFAULT_MIN_RETENTION
                       ) -> dict[str, FactualityCertificate]:
    """One certificate per group (Mondrian). The guarantee then holds conditionally on the
    group, which is what an operator means by "90% on medical questions", and a group too
    small to certify says so instead of borrowing the others' data."""
    if len(answers) != len(groups):
        raise ValueError("answers and groups must be the same length")
    by: dict[str, list[Answer]] = defaultdict(list)
    for a, g in zip(answers, groups):
        by[g].append(a)
    fit = calibrate_strict if promise == "all-supported" else calibrate_expected_share
    return {g: fit(members, alpha, min_retention, group=g) for g, members in sorted(by.items())}
