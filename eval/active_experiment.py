"""Reproduce the active-certification tables (docs/adr/0018). Seeded, no stack, no network.

    python active_experiment.py [--runs 300] [--markdown ../docs/active-results.md]

Traffic: answers whose true groundedness is 1 with probability 0.65, judged by one of three
simulated three-verdict judges (calibrated, flattering, confidently wrong; see
test_active.JUDGES). Every period is 4000 judged answers with a 5% label budget.

Three questions, one table each:

1. Fixed sample: does choosing *which* answers a human labels buy precision, and does it
   stay valid? Uniform sampling (= PPI) against the textbook active rule and the learned
   per-verdict design, which is fitted on a uniform cold-start period.
2. Anytime: what does it cost to be allowed to read the number every day?
3. Claims: what happens to ADR 0017's threshold when it is calibrated on the actively
   chosen answers, with and without reweighting?

What it is not: a measurement of this system's judge. The verdict tables are assumptions,
chosen to span the ways a judge goes wrong; the numbers to believe are the ones
`python active.py` prints on a real review export.
"""
from __future__ import annotations

import argparse
import json
import random
import sys

import active as ac
import factuality as fa
from test_active import JUDGES, THETA, claim_traffic, label, traffic, two_periods

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

N, BUDGET, FLOOR = 4000, 0.05, 0.01
DESIGNS = ("uniform", "textbook", "learned")


def _mean(xs):
    return sum(xs) / len(xs)


def fixed_sample(runs: int) -> dict:
    out = {}
    for judge in JUDGES:
        out[judge] = {}
        for design in DESIGNS:
            cover, widths, labels = 0, [], []
            for r in range(runs):
                rng = random.Random(f"fixed:{judge}:{design}:{r}")
                e = ac.active_mean(*two_periods(rng, judge, N, BUDGET, design, FLOOR))
                cover += e.lo <= THETA <= e.hi
                widths.append(e.width)
                labels.append(e.n_labels)
            out[judge][design] = {"coverage": cover / runs, "width": _mean(widths),
                                  "labels": _mean(labels)}
        uniform = out[judge]["uniform"]["width"]
        for design in DESIGNS:
            cell = out[judge][design]
            # Variance scales as 1/labels, so a width ratio r is worth 1 - r^2 of the labels.
            cell["labels_saved"] = 1.0 - (cell["width"] / uniform) ** 2
    return out


def anytime(runs: int, width_runs: int) -> dict:
    """Both sequences, read after every answer. "Ever excluded" counts a run as a miss if
    the truth fell outside at any one of the 4000 looks, which is what reading a dashboard
    every morning amounts to."""
    out = {}
    for judge in JUDGES:
        ever_exact = ever_asym = 0
        for r in range(runs):
            rng = random.Random(f"any:{judge}:{r}")
            pred, pi, s, l = two_periods(rng, judge, N, BUDGET, "learned", FLOOR)
            book = ac.ActiveCapitalProcess(THETA, alpha=0.05)
            exact_hit = asym_hit = False
            z_sum = z_sq = 0.0
            labels_so_far = 0
            for t, (f, p, sel, y) in enumerate(zip(pred, pi, s, l), start=1):
                lo, hi = ac._z_range(f, p, 1.0)
                z = f + ((y - f) / p if sel else 0.0)
                book.update(z, lo, hi)
                exact_hit = exact_hit or book.rejected
                z_sum += z
                z_sq += z * z
                labels_so_far += sel
                if labels_so_far >= ac.DEFAULT_MIN_LABELS and not asym_hit:
                    mean = z_sum / t
                    sd = max(0.0, (z_sq - t * mean * mean) / (t - 1)) ** 0.5
                    asym_hit = abs(mean - THETA) > sd * ac.asymptotic_radius(t, 0.05)
            ever_exact += exact_hit
            ever_asym += asym_hit
        exact_w, asym_w, fixed_w = [], [], []
        for r in range(width_runs):
            rng = random.Random(f"anyw:{judge}:{r}")
            pred, pi, s, l = two_periods(rng, judge, N, BUDGET, "learned", FLOOR)
            cs = ac.ActiveConfidenceSequence(alpha=0.05, grid=100)
            for row in zip(pred, pi, s, l):
                cs.observe(*row)
            lo, hi = cs.bounds
            alo, ahi = cs.asymptotic_bounds()
            exact_w.append(hi - lo)
            asym_w.append(ahi - alo)
            fixed_w.append(ac.active_mean(pred, pi, s, l).width)
        out[judge] = {"exact_ever_excluded": ever_exact / runs, "exact_width": _mean(exact_w),
                      "asymptotic_ever_excluded": ever_asym / runs,
                      "asymptotic_width": _mean(asym_w), "fixed_width": _mean(fixed_w)}
    return out


def claims(runs: int) -> dict:
    out = {}
    for judge in JUDGES:
        rows = {"naive": [], "weighted": []}
        for r in range(runs):
            rng = random.Random(f"claims:{judge}:{r}")
            items = claim_traffic(rng, 1500, judge)
            pis = ac.inclusion_probabilities([ac.judge_uncertainty(f) for _, _, f in items],
                                             0.15, floor=0.02, mix=0.0)
            chosen = [(a, p) for (a, _, _), p in zip(items, pis) if rng.random() < p]
            test = [a for a, _, _ in claim_traffic(rng, 1500, judge)]
            answers = [a for a, _ in chosen]
            rows["naive"].append(fa.evaluate(test, fa.calibrate_strict(answers, 0.1).threshold))
            cert = ac.calibrate_strict_weighted(answers, [p for _, p in chosen], 0.1, 0.02)
            rows["weighted"].append(fa.evaluate(test, cert.threshold))
        out[judge] = {k: {"all_supported": _mean([e["all_supported"] for e in v]),
                          "retention": _mean([e["retention"] for e in v])}
                      for k, v in rows.items()}
    return out


def markdown(res: dict, runs: int) -> str:
    pct = lambda x: f"{x:.1%}"
    lines = [
        "# Active, anytime-valid certification — simulation results", "",
        "Generated by `make eval-active` (eval/active_experiment.py, seeded). Simulated "
        "judges, not this system's; see [ADR 0018](adr/0018-active-anytime-certification.md).",
        "", f"Truth 0.65 · {N} judged answers per period · {BUDGET:.0%} label budget · "
        f"floor {FLOOR} · {runs} runs per cell", "",
        "## 1. Which answers to label (fixed sample, 95% interval)", "",
        "| judge | design | coverage | width | labels used | labels saved vs uniform |",
        "|---|---|:---:|:---:|:---:|:---:|",
    ]
    for judge, cells in res["fixed"].items():
        for design, c in cells.items():
            saved = "—" if design == "uniform" else f"{c['labels_saved']:+.0%}"
            lines.append(f"| {judge} | {design} | {pct(c['coverage'])} | {c['width']:.4f} | "
                         f"{c['labels']:.0f} | {saved} |")
    lines += ["", "## 2. Reading it every day (learned design, alpha = 5%)", "",
              "Each run is read after every one of its 4000 answers; a run counts as a miss "
              "if the truth fell outside at *any* look (the asymptotic one from its 60th label "
              "on). The fixed-sample interval is valid "
              "for one look only and is shown for its width.", "",
              "| judge | exact: ever excluded | exact: width | asymptotic: ever excluded | "
              "asymptotic: width | fixed-sample width |", "|---|:---:|:---:|:---:|:---:|:---:|"]
    for judge, c in res["anytime"].items():
        lines.append(f"| {judge} | {pct(c['exact_ever_excluded'])} | {c['exact_width']:.3f} | "
                     f"{pct(c['asymptotic_ever_excluded'])} | {c['asymptotic_width']:.3f} | "
                     f"{c['fixed_width']:.3f} |")
    lines += ["", "## 3. The claim threshold, recalibrated on actively chosen answers "
              "(promise: 90% fully supported)", "",
              "| judge | calibration | answers fully supported | supported claims kept |",
              "|---|---|:---:|:---:|"]
    for judge, rows in res["claims"].items():
        for name, c in rows.items():
            lines.append(f"| {judge} | {name} | {pct(c['all_supported'])} | "
                         f"{pct(c['retention'])} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--markdown")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    res = {"fixed": fixed_sample(args.runs),
           "anytime": anytime(max(50, args.runs // 2), max(10, args.runs // 15)),
           "claims": claims(max(30, args.runs // 3))}
    text = markdown(res, args.runs)
    print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
