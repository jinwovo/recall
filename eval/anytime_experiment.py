"""Reproducible experiment behind the anytime-evaluation claims (docs/adr/0012).

Two numbers get quoted in the README, and this script is where both come from. It is
seeded end to end, uses no network and no stack, and rerunning it reproduces the tables
value for value.

**Experiment 1 — what peeking costs.** Simulated evaluation streams from distributions
shaped like real retrieval metrics. After every query, check whether the true mean is
inside the interval. Count the streams where it ever escapes. A 95% fixed-N interval
promises 5% — but only for a single look at a finished sample, and a CI log is not that.
The confidence sequence promises 5% across every look, and the gap between the two columns
is the size of the guarantee nobody was getting.

The comparison is deliberately kind to the fixed-N interval. Peeking starts at
`--min-peek` (default 30) rather than at the second query, because a normal-approximation
interval on two observations is degenerate — zero sample variance collapses it to a point
that excludes almost everything — and counting those would inflate the result with a
failure of the approximation rather than a failure of peeking. What the table shows is
what happens once the interval is in the regime where it is normally considered valid.

**Experiment 2 — what stopping early saves.** Run a threshold gate over the same streams,
stopping as soon as the verdict is determined. Report where it stopped, how much of the
query budget went unspent, and how often it was wrong — because a saving that comes from
answering wrongly is not a saving.

Usage:
    python anytime_experiment.py
    python anytime_experiment.py --markdown anytime.md --json anytime.json
    python anytime_experiment.py --streams 2000       # tighter estimates, slower

Stdlib only.
"""
import argparse
import json
import math
import random
import sys
from pathlib import Path

import sequential as seq

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

Z = 1.959963985                # 95% normal quantile
ALPHA = 0.05
DEFAULT_STREAMS = 800
# Do not start peeking before the normal approximation is defensible; see the module
# docstring. Below this the fixed-N interval fails for reasons unrelated to peeking.
DEFAULT_MIN_PEEK = 30
GATE_BUDGET = 300              # queries a gate is allowed to spend before giving up


# Distributions shaped like the metrics this repo actually reports. Each is (name, mean,
# sampler) — the mean is exact, so coverage is measured against truth, not an estimate.
def _rr_sampler(support, weights):
    return lambda r: r.choices(support, weights)[0]


METRIC_SHAPES = [
    ("Recall@5 (0/1)", 0.85, lambda r: 1.0 if r.random() < 0.85 else 0.0),
    ("reciprocal rank (discrete, skewed)",
     0.6 * 1.0 + 0.2 * 0.5 + 0.1 * (1 / 3) + 0.05 * 0.2 + 0.05 * 0.0,
     _rr_sampler([1.0, 0.5, 1 / 3, 0.2, 0.0], [0.6, 0.2, 0.1, 0.05, 0.05])),
    ("nDCG@10 (graded)", 2 / 3, lambda r: r.betavariate(4, 2)),
    ("groundedness judge (skewed high)", 0.8, lambda r: r.betavariate(8, 2)),
]

STREAM_LENGTHS = (50, 150, 300)

# Gate scenarios: a 0.85 line, and systems sitting various distances from it.
GATE_THRESHOLD = 0.85
GATE_TRUE_MEANS = (0.98, 0.95, 0.92, 0.90, 0.88, 0.85, 0.82, 0.75, 0.60)


class RunningInterval:
    """Fixed-N normal interval, updated in O(1) so peeking can be simulated cheaply."""

    __slots__ = ("n", "_mean", "_m2")

    def __init__(self):
        self.n = 0
        self._mean = 0.0
        self._m2 = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self._mean
        self._mean += delta / self.n
        self._m2 += delta * (x - self._mean)          # Welford

    @property
    def bounds(self) -> tuple[float, float]:
        if self.n < 2:
            return (0.0, 1.0)
        half = Z * math.sqrt((self._m2 / (self.n - 1)) / self.n)
        return (max(0.0, self._mean - half), min(1.0, self._mean + half))


def peeking_experiment(streams: int, min_peek: int = DEFAULT_MIN_PEEK,
                       seed: int = 4242) -> list[dict]:
    """For each metric shape and stream length, how often the truth escapes each interval."""
    rows = []
    for name, truth, sampler in METRIC_SHAPES:
        for length in STREAM_LENGTHS:
            rng = random.Random(f"{seed}:{name}:{length}")
            naive_escapes = anytime_escapes = 0
            for _ in range(streams):
                draws = [sampler(rng) for _ in range(length)]
                fixed = RunningInterval()
                book = seq.CapitalProcess(truth, ALPHA)
                naive_out = anytime_out = False
                for x in draws:
                    fixed.update(x)
                    book.update(x)
                    if not naive_out and fixed.n >= min_peek:
                        lo, hi = fixed.bounds
                        naive_out = not lo <= truth <= hi
                    if not anytime_out:
                        anytime_out = book.rejected
                    if naive_out and anytime_out:
                        break
                naive_escapes += naive_out
                anytime_escapes += anytime_out
            rows.append({
                "metric": name, "true_mean": truth, "queries": length,
                "min_peek": min_peek,
                "fixed_n_escape_rate": naive_escapes / streams,
                "anytime_escape_rate": anytime_escapes / streams,
                "streams": streams,
            })
    return rows


def gate_experiment(streams: int, seed: int = 909) -> list[dict]:
    """Where a sequential gate stops, what it saves, and how often it is wrong."""
    rows = []
    for true_mean in GATE_TRUE_MEANS:
        rng = random.Random(f"{seed}:{true_mean}")
        stops, decisions, wrong = [], {"pass": 0, "fail": 0, "undecided": 0}, 0
        for _ in range(streams):
            values = [1.0 if rng.random() < true_mean else 0.0 for _ in range(GATE_BUDGET)]
            verdict = seq.SequentialGate(GATE_THRESHOLD, "recall@5", ALPHA).run(values)
            stops.append(verdict.queries_used)
            decisions[verdict.decision] += 1
            truly_above = true_mean > GATE_THRESHOLD
            if verdict.decision == "pass" and not truly_above:
                wrong += 1
            elif verdict.decision == "fail" and truly_above:
                wrong += 1
        stops.sort()
        decided = decisions["pass"] + decisions["fail"]
        rows.append({
            "true_mean": true_mean,
            "threshold": GATE_THRESHOLD,
            "median_stop": stops[len(stops) // 2],
            "mean_stop": sum(stops) / len(stops),
            "budget": GATE_BUDGET,
            "saved_fraction": 1.0 - (sum(stops) / len(stops)) / GATE_BUDGET,
            "decided_fraction": decided / streams,
            "wrong_fraction": wrong / streams,
            "decisions": decisions,
            "streams": streams,
        })
    return rows


def render(peeking: list[dict], gates: list[dict], markdown: bool) -> str:
    out: list[str] = []
    add = out.append
    streams = peeking[0]["streams"] if peeking else 0

    if markdown:
        add("## Anytime-valid evaluation — measured")
        add("")
        add(f"Simulated evaluation streams, {streams:,} per cell, seeded. "
            "Reproduce with `python eval/anytime_experiment.py`.")
        add("")
        add(f"Peeking starts at query {peeking[0]['min_peek']} so the fixed-N interval is "
            "judged in the regime where it is normally considered valid — counting its "
            "degenerate small-sample behaviour would inflate the comparison.")
        add("")
        add("### 1. What peeking costs")
        add("")
        add("Share of streams where the true mean falls outside a nominal **95%** interval "
            "at *some* point while it is being watched — which is how a CI log is read.")
        add("")
        add("| metric | queries | fixed-N interval | confidence sequence |")
        add("|---|:---:|:---:|:---:|")
        for r in peeking:
            add(f"| {r['metric']} | {r['queries']} | "
                f"**{r['fixed_n_escape_rate']:.1%}** | {r['anytime_escape_rate']:.1%} |")
        add("")
        add("The fixed-N column is not a bug in the interval; it is the interval being used "
            "for something it never promised. The right-hand column is a promise that holds "
            "at every stopping time, which is what makes stopping early legitimate.")
        add("")
        add("### 2. What stopping early saves")
        add("")
        add(f"A gate asking `Recall@5 >= {GATE_THRESHOLD}` with a budget of "
            f"{GATE_BUDGET} queries, stopped as soon as the verdict is determined.")
        add("")
        add("| true Recall@5 | verdict | decided | mean stop | queries saved | wrong |")
        add("|:---:|:---:|:---:|:---:|:---:|:---:|")
        for r in gates:
            majority = max(r["decisions"], key=lambda k: r["decisions"][k])
            note = "at the line" if r["true_mean"] == r["threshold"] else majority
            add(f"| {r['true_mean']:.2f} | {note} | {r['decided_fraction']:.0%} "
                f"| {r['mean_stop']:.0f} | **{r['saved_fraction']:.0%}** "
                f"| {r['wrong_fraction']:.1%} |")
        add("")
        add("A system at the line is correctly reported `undecided` rather than nudged "
            "either way, and spends its whole budget doing so — the one case where there "
            "is nothing to save and nothing to claim.")
    else:
        add(f"Anytime-valid evaluation — {streams:,} streams per cell\n")
        add("1. Escape rate under continuous inspection (nominal 95%)")
        add(f"   {'metric':<40}{'n':>6}{'fixed-N':>10}{'anytime':>10}")
        for r in peeking:
            add(f"   {r['metric']:<40}{r['queries']:>6}"
                f"{r['fixed_n_escape_rate']:>9.1%}{r['anytime_escape_rate']:>10.1%}")
        add("")
        add(f"2. Sequential gate, Recall@5 >= {GATE_THRESHOLD}, budget {GATE_BUDGET}")
        add(f"   {'true':>6}{'mean stop':>11}{'median':>8}{'saved':>8}{'decided':>9}{'wrong':>8}")
        for r in gates:
            add(f"   {r['true_mean']:>6.2f}{r['mean_stop']:>11.0f}{r['median_stop']:>8}"
                f"{r['saved_fraction']:>8.0%}{r['decided_fraction']:>9.0%}"
                f"{r['wrong_fraction']:>8.1%}")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--streams", type=int, default=DEFAULT_STREAMS,
                        help="simulated evaluation streams per cell")
    parser.add_argument("--min-peek", type=int, default=DEFAULT_MIN_PEEK,
                        help="first query at which the fixed-N interval is inspected")
    parser.add_argument("--markdown", metavar="PATH", help="write the tables as markdown")
    parser.add_argument("--json", metavar="PATH", help="write the raw measurements")
    args = parser.parse_args()

    peeking = peeking_experiment(args.streams, args.min_peek)
    gates = gate_experiment(args.streams)
    print(render(peeking, gates, markdown=False))

    if args.markdown:
        Path(args.markdown).write_text(render(peeking, gates, markdown=True), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"alpha": ALPHA, "streams": args.streams, "peeking": peeking, "gates": gates},
            indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
