"""Reproduce the claim-level factuality tables (docs/adr/0017). Seeded, no stack, no network.

    python factuality_experiment.py [--runs 300] [--json ../docs/factuality-results.json]

Answers are 2-8 atomic claims; a claim is false with probability 0.2 ("easy") or 0.45
("hard"). An entailment-style scorer gives supported claims Beta(5,2) and false ones
Beta(2,5). Every cell is the mean over `runs` calibrate-on-300 / deploy-on-1000 cycles, so
it is a property of the procedure, not of one lucky split.

What it is not: a measurement of this system's scorer. The cross-encoder's real claim
scores need decomposed answers with human claim labels, which do not exist yet — the
calibration path for them is `calibrate_strict` on a labelled JSONL, and the numbers it
prints are the ones to believe over these.
"""
from __future__ import annotations

import argparse
import json
import random

import factuality as fa
from test_factuality import sample

ALPHA = 0.10
N_CAL, N_TEST = 300, 1000


def _mean(xs):
    return sum(xs) / len(xs)


def promises(rng, runs):
    """Strict vs expected-share, on the same draws."""
    rows = {"all-supported": [], "expected-share": []}
    for _ in range(runs):
        cal, test = sample(rng, N_CAL), sample(rng, N_TEST)
        for name, fit in (("all-supported", fa.calibrate_strict),
                          ("expected-share", fa.calibrate_expected_share)):
            rows[name].append(fa.evaluate(test, fit(cal, alpha=ALPHA).threshold))
    unfiltered = fa.evaluate(sample(rng, N_TEST), -float("inf"))
    out = {"no filter": unfiltered}
    for name, evs in rows.items():
        out[name] = {k: _mean([e[k] for e in evs]) for k in
                     ("all_supported", "false_kept_share", "retention", "vacuous_rate")}
    return out


def groups(rng, runs):
    """Marginal vs per-group calibration, read out on each group separately."""
    res = {"marginal": {"easy": [], "hard": []}, "per-group": {"easy": [], "hard": []}}
    for _ in range(runs):
        easy, hard = sample(rng, N_CAL // 2, p_false=0.05), sample(rng, N_CAL // 2, p_false=0.45)
        marginal = fa.calibrate_strict(easy + hard, alpha=ALPHA)
        per = fa.calibrate_by_group(easy + hard, ["easy"] * len(easy) + ["hard"] * len(hard),
                                    alpha=ALPHA)
        tests = {"easy": sample(rng, N_TEST // 2, p_false=0.05),
                 "hard": sample(rng, N_TEST // 2, p_false=0.45)}
        for g, t in tests.items():
            res["marginal"][g].append(fa.evaluate(t, marginal.threshold))
            res["per-group"][g].append(fa.evaluate(t, per[g].threshold))
    return {how: {g: {"all_supported": _mean([e["all_supported"] for e in evs]),
                      "retention": _mean([e["retention"] for e in evs])}
                  for g, evs in by.items()}
            for how, by in res.items()}


def shift(rng, runs):
    """Calibrated on clean retrieval, deployed against distractors that make false claims
    look entailed — the failure arXiv 2603.16817 reports."""
    clean, shifted = [], []
    for _ in range(runs):
        cert = fa.calibrate_strict(sample(rng, N_CAL), alpha=ALPHA)
        clean.append(fa.evaluate(sample(rng, N_TEST), cert.threshold)["all_supported"])
        shifted.append(fa.evaluate(sample(rng, N_TEST, distractor=True),
                                   cert.threshold)["all_supported"])
    return {"clean": _mean(clean), "distractors": _mean(shifted)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)

    p, g, s = promises(rng, args.runs), groups(rng, args.runs), shift(rng, args.runs)
    pct = lambda x: f"{x:.1%}"

    print(f"alpha = {ALPHA}, calibrate on {N_CAL}, deploy on {N_TEST}, {args.runs} runs\n")
    print("| filter | answers fully supported | false-and-kept share | supported claims kept | answers emptied |")
    print("|---|:---:|:---:|:---:|:---:|")
    for name, e in p.items():
        print(f"| {name} | {pct(e['all_supported'])} | {pct(e['false_kept_share'])} | "
              f"{pct(e['retention'])} | {pct(e['vacuous_rate'])} |")
    print("\n| calibration | easy: fully supported | hard: fully supported | hard: claims kept |")
    print("|---|:---:|:---:|:---:|")
    for how, by in g.items():
        print(f"| {how} | {pct(by['easy']['all_supported'])} | {pct(by['hard']['all_supported'])} "
              f"| {pct(by['hard']['retention'])} |")
    print(f"\nshift: {pct(s['clean'])} fully supported on clean retrieval, "
          f"{pct(s['distractors'])} with distractors (promised {pct(1 - ALPHA)})")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"alpha": ALPHA, "n_calibration": N_CAL, "n_test": N_TEST,
                       "runs": args.runs, "seed": args.seed,
                       "promises": p, "groups": g, "shift": s}, f, indent=2)


if __name__ == "__main__":
    main()
