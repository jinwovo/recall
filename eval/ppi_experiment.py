"""Reproducible experiment behind the prediction-powered inference claims (docs/adr/0015).

Simulates the situation every RAG evaluation is actually in: a judge model grades thousands
of answers cheaply, a human grades a few dozen expensively, and a number gets published.
Three ways to publish it are compared, over many independent repetitions of the whole
labelling-and-estimating cycle:

    judge only      average the judge's scores. What almost every report does.
    labels only     average the hand labels, with the interval that sample supports.
    PPI             both, combined so the judge's bias is measured and subtracted.

The quantity being estimated is fixed and known, so "did the interval contain it" is a
fact rather than an opinion. What comes out is a coverage rate — how often each method's
95% interval actually held — and a width, because an interval that covers by being
uselessly wide has not solved anything.

Usage:
    python ppi_experiment.py
    python ppi_experiment.py --markdown ppi.md --json ppi.json
    python ppi_experiment.py --trials 4000 --labeled 100

Stdlib only, seeded.
"""
import argparse
import json
import random
import sys
from pathlib import Path

import ppi

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

# Beta(5, 2) — a graded score bunched toward the top, which is what groundedness looks like
# on a system that mostly works. Its mean is exactly 5/7, so the estimand needs no estimating.
TRUE_MEAN = 5 / 7
DEFAULT_TRIALS = 1500
DEFAULT_LABELED = 50
DEFAULT_UNLABELED = 2000

# (name, bias, noise, signal) — how the judge relates to the truth it is standing in for.
JUDGES = [
    ("flattering, accurate", 0.08, 0.05, 1.0),
    ("flattering, noisy", 0.08, 0.20, 1.0),
    ("unbiased, noisy", 0.00, 0.20, 1.0),
    ("harsh, accurate", -0.12, 0.05, 1.0),
    ("uninformative", 0.00, 0.30, 0.0),
]


def draw(rng: random.Random, size: int, bias: float, noise: float, signal: float):
    """(truth, judge prediction) pairs for `size` items."""
    out = []
    for _ in range(size):
        truth = rng.betavariate(5, 2)
        prediction = signal * truth + bias + rng.gauss(0.0, noise)
        out.append((truth, min(1.0, max(0.0, prediction))))
    return out


def run_judge(name: str, bias: float, noise: float, signal: float, trials: int,
              n_labeled: int, n_unlabeled: int, seed: int) -> dict:
    covered = {"judge": 0, "labels": 0, "ppi": 0}
    widths = {"judge": [], "labels": [], "ppi": []}
    estimates = {"judge": [], "ppi": []}
    lambdas, effective = [], []

    for trial in range(trials):
        rng = random.Random(f"{seed}:{name}:{trial}")
        pairs = draw(rng, n_labeled + n_unlabeled, bias, noise, signal)
        labels = [y for y, _ in pairs[:n_labeled]]
        labeled_pred = [f for _, f in pairs[:n_labeled]]
        unlabeled_pred = [f for _, f in pairs[n_labeled:]]

        estimate = ppi.ppi_mean(labels, labeled_pred, unlabeled_pred)
        covered["ppi"] += estimate.lo <= TRUE_MEAN <= estimate.hi
        widths["ppi"].append(estimate.width)
        estimates["ppi"].append(estimate.estimate)
        lambdas.append(estimate.lam)
        effective.append(estimate.effective_n)

        lo, hi = estimate.labeled_only_ci
        covered["labels"] += lo <= TRUE_MEAN <= hi
        widths["labels"].append(hi - lo)

        # The naive report: average every judge score and attach the interval that many
        # observations would normally justify.
        all_predictions = labeled_pred + unlabeled_pred
        jlo, jhi = ppi.classical_interval(all_predictions)
        covered["judge"] += jlo <= TRUE_MEAN <= jhi
        widths["judge"].append(jhi - jlo)
        estimates["judge"].append(sum(all_predictions) / len(all_predictions))

    mean = lambda xs: sum(xs) / len(xs)                                   # noqa: E731
    return {
        "judge": name, "bias": bias, "noise": noise, "signal": signal,
        "coverage": {k: v / trials for k, v in covered.items()},
        "width": {k: mean(v) for k, v in widths.items()},
        "estimate": {k: mean(v) for k, v in estimates.items()},
        "lambda": mean(lambdas), "effective_n": mean(effective),
        "width_ratio": mean(widths["ppi"]) / mean(widths["labels"]),
    }


def render(rows: list[dict], settings: dict, markdown: bool) -> str:
    out: list[str] = []
    add = out.append
    n, N, trials = settings["labeled"], settings["unlabeled"], settings["trials"]

    if markdown:
        add("## Prediction-powered inference — measured")
        add("")
        add(f"{trials:,} independent repetitions per row: draw a population, hand-label "
            f"**{n}** items, let the judge score all **{n + N:,}**, publish a 95% interval, "
            f"check whether it contained the truth. The estimand is the mean of Beta(5, 2), "
            f"which is exactly 5/7 = {TRUE_MEAN:.4f}.")
        add("")
        add("| judge behaviour | judge only | hand labels only | **PPI** |")
        add("|---|:---:|:---:|:---:|")
        for r in rows:
            add(f"| {r['judge']} | {r['coverage']['judge']:.1%} "
                f"| {r['coverage']['labels']:.1%} | **{r['coverage']['ppi']:.1%}** |")
        add("")
        add("*Coverage: how often the published 95% interval actually contained the true "
            "value.* Averaging the judge is not a 95% interval — it is a narrow interval "
            "around whatever the judge believes, and when the judge is biased it is almost "
            "never right. PPI holds at the nominal rate regardless.")
        add("")
        add("| judge behaviour | width, labels only | width, PPI | narrower by | λ | "
            f"effective labels (from {n}) |")
        add("|---|:---:|:---:|:---:|:---:|:---:|")
        for r in rows:
            add(f"| {r['judge']} | {r['width']['labels']:.4f} | {r['width']['ppi']:.4f} "
                f"| **{1 - r['width_ratio']:.0%}** | {r['lambda']:.2f} "
                f"| **{r['effective_n']:.0f}** |")
        add("")
        add("*The cost of a bad judge is zero, not negative:* the uninformative row drives "
            "λ to zero and lands back on the hand-label interval. Everything above it is "
            "precision bought without writing more labels.")
    else:
        add(f"Prediction-powered inference — {trials:,} trials per row")
        add(f"  hand labels: {n}   judge-scored: {n + N:,}   true mean: {TRUE_MEAN:.4f}\n")
        add(f"  {'judge behaviour':<24}{'cover(judge)':>13}{'cover(labels)':>15}"
            f"{'cover(PPI)':>12}")
        for r in rows:
            add(f"  {r['judge']:<24}{r['coverage']['judge']:>12.1%}"
                f"{r['coverage']['labels']:>15.1%}{r['coverage']['ppi']:>12.1%}")
        add("")
        add(f"  {'judge behaviour':<24}{'w(labels)':>11}{'w(PPI)':>9}{'narrower':>10}"
            f"{'lambda':>8}{'eff. n':>8}")
        for r in rows:
            add(f"  {r['judge']:<24}{r['width']['labels']:>11.4f}{r['width']['ppi']:>9.4f}"
                f"{1 - r['width_ratio']:>9.0%}{r['lambda']:>8.2f}{r['effective_n']:>8.0f}")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--labeled", type=int, default=DEFAULT_LABELED,
                        help="items a human grades per trial")
    parser.add_argument("--unlabeled", type=int, default=DEFAULT_UNLABELED,
                        help="items only the judge grades")
    parser.add_argument("--seed", type=int, default=90210)
    parser.add_argument("--markdown", metavar="PATH")
    parser.add_argument("--json", metavar="PATH")
    args = parser.parse_args()

    rows = [run_judge(name, bias, noise, signal, args.trials, args.labeled,
                      args.unlabeled, args.seed)
            for name, bias, noise, signal in JUDGES]
    settings = {"trials": args.trials, "labeled": args.labeled,
                "unlabeled": args.unlabeled, "true_mean": TRUE_MEAN, "seed": args.seed}

    print(render(rows, settings, markdown=False))
    if args.markdown:
        Path(args.markdown).write_text(render(rows, settings, markdown=True), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({"settings": settings, "rows": rows}, indent=2),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
