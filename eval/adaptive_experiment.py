"""Reproducible experiment behind the adaptive conformal claims (docs/adr/0016).

ADR 0013 calibrates a conformal threshold once and writes it into `application.yml`. That
number is only meaningful while the scores it was calibrated on and the scores it will serve
come from the same distribution — and a live corpus stops satisfying that on its second day.
This measures what actually happens, over many independent streams, to three arrangements:

    fixed       calibrate once, apply forever. The ADR 0013 arrangement.
    window      recalibrate continuously on a rolling window of recent scores.
    adaptive    the window, plus the ACI level controller closing the loop on miscoverage.

Reported for each: realised coverage against the 90% it promises, and — for the adaptive
one — the standing offset the level had to hold, which is the part that says *why*.

The headline is not the one this experiment was written to find. Against a pure location
shift the rolling window alone is enough: it does essentially all the work, and the level
controller has nothing left to correct. The controller earns its place on the second
scenario, where the score distribution changes shape rather than position and no rolling
quantile can track it.

Usage:
    python adaptive_experiment.py
    python adaptive_experiment.py --markdown adaptive.md --json adaptive.json
    python adaptive_experiment.py --trials 400

Stdlib only, seeded.
"""
import argparse
import json
import random
import statistics
import sys
from pathlib import Path

import adaptive
from conformal import conformal_quantile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ALPHA = 0.10                  # promise 90% coverage
GAMMA = 0.05
WINDOW = 600
CALIBRATION = 600
LIVE = 3000
SHIFT_AT = LIVE // 3
DEFAULT_TRIALS = 200
SPREAD = 0.15


def draw(rng: random.Random, mean: float, spread: float = SPREAD) -> float:
    """One nonconformity score, clamped to the unit interval the reranker reports in."""
    return min(1.0, max(0.0, rng.gauss(mean, spread)))


# -- the two ways a live corpus stops being exchangeable ------------------------------

def location_stream(rng: random.Random, shift: float) -> tuple[list[float], list[float]]:
    """The corpus moves: scores keep their shape and slide upward.

    A reindex, a model swap, a change in query mix. The obvious failure, and — as it turns
    out — the recoverable one.
    """
    calibration = [draw(rng, 0.50) for _ in range(CALIBRATION)]
    live = [draw(rng, 0.50 + (shift if step >= SHIFT_AT else 0.0)) for step in range(LIVE)]
    return calibration, live


def saturation_stream(rng: random.Random, mean: float = 1.05) -> tuple[list[float], list[float]]:
    """The reranker saturates: most items pile onto one tied score at the top of its range.

    Real, and nastier than it looks. Once the majority of candidates share the maximum
    score no threshold separates them, so the empirical quantile stops being informative
    however recent the window it was computed from.
    """
    calibration = [draw(rng, 0.50) for _ in range(CALIBRATION)]
    live = [draw(rng, 0.50 if step < SHIFT_AT else mean) for step in range(LIVE)]
    return calibration, live


SCENARIOS = (
    ("location shift (+0.30)", lambda rng: location_stream(rng, 0.30)),
    ("score saturation", lambda rng: saturation_stream(rng)),
)


# -- the three arrangements -------------------------------------------------------------

def fixed_coverage(calibration: list[float], live: list[float]) -> float:
    threshold = conformal_quantile(calibration, ALPHA)
    return sum(1 for score in live if score <= threshold) / len(live)


def window_coverage(calibration: list[float], live: list[float]) -> float:
    """Rolling recalibration at a level that never moves — the window without the loop."""
    from collections import deque
    scores: deque[float] = deque(calibration[-WINDOW:], maxlen=WINDOW)
    covered = 0
    for score in live:
        if score <= conformal_quantile(list(scores), ALPHA):
            covered += 1
        scores.append(score)
    return covered / len(live)


def adaptive_run(calibration: list[float], live: list[float]) -> adaptive.AdaptiveConformal:
    controller = adaptive.AdaptiveConformal(ALPHA, GAMMA, window=WINDOW)
    for score in calibration:
        controller.scores.append(score)          # seed the window; no feedback yet
    for score in live:
        controller.observe(score)
    return controller


def recovery_steps(calibration: list[float], live: list[float]) -> int | None:
    """Queries after the change before the trailing miss rate is back on target.

    A controller that converges eventually but takes ten thousand queries to notice is no
    use to a system that reindexes nightly, so this is reported in queries, not in theory.
    """
    controller = adaptive.AdaptiveConformal(ALPHA, GAMMA, window=WINDOW)
    for score in calibration:
        controller.scores.append(score)
    outcomes = [0 if controller.observe(score) else 1 for score in live]
    # Measured with a trailing window of 100 anchored at the change itself, so a recovery
    # faster than the window is reported as the 0 it is rather than hidden by the span.
    span = 100
    for end in range(SHIFT_AT + span, len(outcomes)):
        recent = outcomes[end - span:end]
        if abs(sum(recent) / span - ALPHA) < 0.04:
            return end - span - SHIFT_AT
    return None


def trial(seed: int, build) -> dict:
    calibration, live = build(random.Random(seed))
    controller = adaptive_run(calibration, live)
    return {
        "fixed": fixed_coverage(calibration, live),
        "window": window_coverage(calibration, live),
        "adaptive": 1.0 - controller.realized_miscoverage,
        "offset": abs(controller.alpha_t - ALPHA),
        "compensating": controller.compensating,
        "within_bound": controller.within_bound,
        "recovery": recovery_steps(calibration, live),
    }


def summarise(name: str, build, trials: int, seed: int) -> dict:
    runs = [trial(seed + index, build) for index in range(trials)]
    recoveries = [run["recovery"] for run in runs if run["recovery"] is not None]
    return {
        "scenario": name,
        "trials": trials,
        "fixed": statistics.mean(run["fixed"] for run in runs),
        "fixed_worst": min(run["fixed"] for run in runs),
        "window": statistics.mean(run["window"] for run in runs),
        "adaptive": statistics.mean(run["adaptive"] for run in runs),
        "offset": statistics.mean(run["offset"] for run in runs),
        "compensating": sum(run["compensating"] for run in runs) / trials,
        "within_bound": sum(run["within_bound"] for run in runs) / trials,
        "recovery_median": statistics.median(recoveries) if recoveries else None,
        "recovered": len(recoveries) / trials,
    }


def render(rows: list[dict], settings: dict, markdown: bool) -> str:
    out: list[str] = []
    if markdown:
        out += [f"Coverage promised: {1 - ALPHA:.0%}. "
                f"{settings['trials']} independent streams per scenario, "
                f"{LIVE} queries each, change at query {SHIFT_AT}.", "",
                "| scenario | fixed | window | adaptive | level offset | compensating |",
                "|---|---|---|---|---|---|"]
    else:
        out += [f"coverage promised: {1 - ALPHA:.0%}   "
                f"{settings['trials']} streams x {LIVE} queries, change at {SHIFT_AT}", "",
                f"{'scenario':<24}{'fixed':>9}{'window':>9}{'adaptive':>10}"
                f"{'offset':>9}{'compens.':>10}"]
    for row in rows:
        cells = [f"{row['scenario']:<24}" if not markdown else row["scenario"],
                 f"{row['fixed']:.3f}", f"{row['window']:.3f}", f"{row['adaptive']:.3f}",
                 f"{row['offset']:.3f}", f"{row['compensating']:.0%}"]
        if markdown:
            out.append("| " + " | ".join(cells) + " |")
        else:
            out.append(cells[0] + "".join(f"{c:>9}" if i < 3 else f"{c:>10}"
                                          for i, c in enumerate(cells[1:], 1)))
    out.append("")
    for row in rows:
        if row["recovery_median"] is None:
            recovery = "not within the stream"
        elif row["recovery_median"] == 0:
            # Already on target in the first trailing window, i.e. faster than this
            # measurement can resolve. Said plainly rather than printed as a bare zero.
            recovery = f"under 100 queries ({row['recovered']:.0%} of streams)"
        else:
            recovery = (f"{row['recovery_median']:.0f} queries "
                        f"({row['recovered']:.0%} of streams)")
        out.append(f"{row['scenario']}: recovery {recovery}; "
                   f"deterministic bound held in {row['within_bound']:.0%}; "
                   f"worst single fixed-threshold stream {row['fixed_worst']:.3f}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                        help="independent streams per scenario")
    parser.add_argument("--seed", type=int, default=54321)
    parser.add_argument("--markdown", metavar="PATH")
    parser.add_argument("--json", metavar="PATH")
    args = parser.parse_args()

    rows = [summarise(name, build, args.trials, args.seed) for name, build in SCENARIOS]
    settings = {"trials": args.trials, "alpha": ALPHA, "gamma": GAMMA, "window": WINDOW,
                "live": LIVE, "shift_at": SHIFT_AT, "seed": args.seed}

    print(render(rows, settings, markdown=False))
    if args.markdown:
        Path(args.markdown).write_text(render(rows, settings, markdown=True), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({"settings": settings, "rows": rows}, indent=2),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
