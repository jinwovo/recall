"""What a gold set can resolve — computed before anything is retrieved (docs/adr/0011).

Every other tool here tells you what a system scored. This one tells you what your gold
set is capable of telling you, which is a property of the labels alone and is knowable
before the stack is even up. It exists because the most expensive mistake in retrieval
work is shipping a change on a measurement that could never have detected it.

Three things get answered:

1. **The p-value floor.** A paired randomization test on k differing queries cannot return
   a two-sided p below 2 / 2^k. Ties are the binding constraint, not the query count: on
   ten queries where seven behave identically, the floor is 0.25 and no result — however
   large — reaches significance.
2. **The interval you are stuck with.** With one relevant document per query, Recall@k is
   a binomial proportion, and the width of its exact interval at a given n is fixed in
   advance. At n = 10, a flawless Recall@5 still only establishes >= 0.69.
3. **The queries you would need.** For a target improvement and a per-query spread, the
   paired sample size at 80% power — the number that decides whether a tuning loop is
   measuring anything.

With `--from-json` (a previous `run_eval.py --json`) the spread is the observed one and
the sample-size answer is specific to this corpus rather than illustrative.

Usage:
    python power_report.py gold.jsonl
    python power_report.py gold.jsonl --from-json rag-eval-results.json
    python power_report.py gold.jsonl --markdown design.md

Stdlib only.
"""
import argparse
import json
import sys
from pathlib import Path

import stats

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

ALPHA = 0.05
POWER = 0.80
# Improvements a retrieval change plausibly delivers, from "not worth a PR" upward.
TARGET_EFFECTS = (0.01, 0.02, 0.05, 0.10, 0.20)
# Per-query difference spreads to bracket the answer when no measured run is supplied.
ASSUMED_SPREADS = (0.20, 0.30, 0.40)
# Recall values worth pricing an interval for, including the deceptive perfect score.
ILLUSTRATIVE_RATES = (1.00, 0.95, 0.90, 0.80, 0.70)


def load_gold(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def gold_shape(examples: list[dict]) -> dict:
    counts = [len(ex.get("relevant_doc_ids", [])) for ex in examples]
    return {
        "n": len(examples),
        "min_relevant": min(counts) if counts else 0,
        "max_relevant": max(counts) if counts else 0,
        "mean_relevant": stats.mean(counts),
        # One relevant document per query makes Recall@k a 0/1 outcome, so the exact
        # binomial interval applies rather than a bootstrap over a degenerate sample.
        "recall_is_binomial": bool(counts) and max(counts) == 1,
    }


def p_floor_ladder(n: int) -> list[dict]:
    """Floor p-value as a function of how many queries actually differ."""
    rows = []
    for k in range(1, n + 1):
        floor = stats.min_attainable_p(k)
        rows.append({"differing": k, "floor_p": floor, "can_reach_significance": floor < ALPHA})
    return rows


def interval_ladder(n: int) -> list[dict]:
    """One row per attainable success count — a small n cannot express every rate.

    At n = 10 there is no 0.95: it rounds onto the same 10/10 as a perfect score, which is
    itself part of the point being made about resolution.
    """
    rows = []
    for successes in sorted({round(rate * n) for rate in ILLUSTRATIVE_RATES}, reverse=True):
        if not 0 <= successes <= n:
            continue
        lo, hi = stats.clopper_pearson(successes, n, ALPHA)
        rows.append({"observed": successes / n, "successes": successes,
                     "lo": lo, "hi": hi, "width": hi - lo})
    return rows


def sample_size_table(spreads, effects) -> list[dict]:
    return [{"sd": sd, "effect": effect,
             "queries": stats.required_queries(effect, sd, ALPHA, POWER)}
            for sd in spreads for effect in effects]


def observed_spreads(results_path: str) -> dict:
    """Per-query difference spread for each mode against the run's baseline mode.

    Reads a `run_eval.py --json` payload, so the sample-size advice uses the variance this
    corpus actually produces instead of an assumed one.
    """
    with open(results_path, encoding="utf-8") as f:
        payload = json.load(f)
    modes = payload.get("modes", {})
    if not modes:
        raise SystemExit(f"{results_path}: no 'modes' block — is this a run_eval.py --json file?")
    baseline = (payload.get("comparison") or {}).get("baseline") or next(iter(modes))

    def rr_values(mode: str) -> list[float]:
        return [float(q["rr"]) for q in modes[mode]["per_query"]]

    base = rr_values(baseline)
    out = {"baseline": baseline, "metric": "mrr@10", "modes": {}}
    for mode in modes:
        if mode == baseline:
            continue
        d = stats.design_analysis(rr_values(mode), base, ALPHA, POWER)
        out["modes"][mode] = {"sd": d.sd, "mde": d.min_detectable_effect,
                              "floor_p": d.min_attainable_p}
    return out


def render(shape: dict, observed: dict | None, markdown: bool) -> str:
    n = shape["n"]
    h1, h2 = ("# ", "## ") if markdown else ("", "")
    out: list[str] = []
    add = out.append

    add(f"{h1}Gold-set design analysis")
    add("")
    add(f"- queries: **{n}**" if markdown else f"queries: {n}")
    relevant = (f"- relevant documents per query: {shape['mean_relevant']:.2f} "
                f"(min {shape['min_relevant']}, max {shape['max_relevant']})")
    add(relevant if markdown else relevant[2:])
    binom = ("Recall@k is a binomial proportion here, so it gets the exact interval"
             if shape["recall_is_binomial"] else
             "queries have multiple relevant documents, so Recall@k is graded and "
             "gets the bootstrap interval")
    add(f"- {binom}" if markdown else binom)
    add("")

    add(f"{h2}1. The p-value floor")
    add("")
    text = (f"A paired randomization test on k differing queries cannot return a two-sided "
            f"p below 2 / 2^k. Ties are what bind: queries that score identically under "
            f"both systems carry no signal, so k, not {n}, is the sample size that counts.")
    add(text)
    add("")
    ladder = p_floor_ladder(n)
    first = next((r for r in ladder if r["can_reach_significance"]), None)
    # The ladder is only interesting where it crosses. On a 300-query set the remaining
    # rows are all "yes" and all astronomically small, and printing them buries the point.
    cutoff = min(len(ladder), (first["differing"] + 1) if first else len(ladder))
    shown, rest = ladder[:cutoff], ladder[cutoff:]
    if markdown:
        add("| differing queries | floor p | can reach p < 0.05 |")
        add("|:---:|:---:|:---:|")
        for r in shown:
            add(f"| {r['differing']} | {r['floor_p']:.4f} | "
                f"{'yes' if r['can_reach_significance'] else '**no**'} |")
        if rest:
            add(f"| {rest[0]['differing']}–{rest[-1]['differing']} | "
                f"{rest[-1]['floor_p']:.2e} … | yes |")
    else:
        for r in shown:
            mark = "" if r["can_reach_significance"] else "   <- cannot reach significance"
            add(f"  k={r['differing']:>4}  floor p = {r['floor_p']:.4f}{mark}")
        if rest:
            add(f"  k={rest[0]['differing']:>4}..{rest[-1]['differing']}  "
                f"floor p down to {rest[-1]['floor_p']:.2e}")
    add("")
    if first:
        k = f"**{first['differing']}**" if markdown else str(first["differing"])
        add(f"At least {k} of {n} queries must differ before any outcome can be significant.")
    else:
        add(f"No number of differing queries reaches p < 0.05 at n = {n}. This gold set "
            f"cannot support a significance claim at all.")
    add("")

    add(f"{h2}2. The interval you are stuck with")
    add("")
    add(f"Exact binomial (Clopper-Pearson) 95% intervals at n = {n}. These widths are a "
        f"property of the sample size, not of the system — they are known before it runs.")
    add("")
    if markdown:
        add("| observed | 95% interval | width |")
        add("|:---:|:---:|:---:|")
        for r in interval_ladder(n):
            add(f"| {r['observed']:.2f} ({r['successes']}/{n}) "
                f"| [{r['lo']:.3f}, {r['hi']:.3f}] | {r['width']:.3f} |")
    else:
        add(f"  {'observed':>12}  {'95% interval':>16}  {'width':>6}")
        for r in interval_ladder(n):
            label = f"{r['observed']:.2f} ({r['successes']}/{n})"
            add(f"  {label:>12}  [{r['lo']:.3f}, {r['hi']:.3f}]  {r['width']:>6.3f}")
    add("")

    add(f"{h2}3. The queries you would need")
    add("")
    if observed:
        add(f"Per-query spread measured on this corpus (`{observed['metric']}` vs "
            f"`{observed['baseline']}`), so these counts are specific, not illustrative.")
        add("")
        spreads = sorted({round(v["sd"], 3) for v in observed["modes"].values()}) or list(ASSUMED_SPREADS)
        if markdown:
            add("| mode | per-query sd | smallest detectable effect | floor p |")
            add("|---|:---:|:---:|:---:|")
            for mode, v in observed["modes"].items():
                add(f"| `{mode}` | {v['sd']:.3f} | {v['mde']:+.3f} | {v['floor_p']:.4f} |")
        else:
            for mode, v in observed["modes"].items():
                add(f"  {mode:<12} sd={v['sd']:.3f}  MDE={v['mde']:+.3f}  "
                    f"floor p={v['floor_p']:.4f}")
        add("")
    else:
        add("No measured run supplied (`--from-json`), so the spread is bracketed across "
            "plausible values.")
        add("")
        spreads = list(ASSUMED_SPREADS)

    rows = sample_size_table(spreads, TARGET_EFFECTS)
    if markdown:
        add("| per-query sd | " + " | ".join(f"Δ = {e:.2f}" for e in TARGET_EFFECTS) + " |")
        add("|:---:|" + ":---:|" * len(TARGET_EFFECTS))
        for sd in spreads:
            cells = [f"{r['queries']:,}" for r in rows if r["sd"] == sd]
            add(f"| {sd:.2f} | " + " | ".join(cells) + " |")
    else:
        add(f"  queries needed at {POWER:.0%} power, alpha = {ALPHA}")
        add("  " + f"{'sd':>6}" + "".join(f"{'d=' + format(e, '.2f'):>12}" for e in TARGET_EFFECTS))
        for sd in spreads:
            cells = "".join(f"{r['queries']:>12,}" for r in rows if r["sd"] == sd)
            add(f"  {sd:>6.2f}{cells}")
    add("")
    add("A tuning loop that acts on a difference smaller than the smallest detectable "
        "effect is not tuning — it is resampling noise.")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="gold.jsonl",
                        help="JSONL gold set to analyse")
    parser.add_argument("--from-json", metavar="PATH",
                        help="a run_eval.py --json result, for the measured per-query spread")
    parser.add_argument("--markdown", metavar="PATH",
                        help="write the report as markdown (step-summary friendly)")
    args = parser.parse_args()

    shape = gold_shape(load_gold(args.gold))
    if shape["n"] == 0:
        raise SystemExit(f"{args.gold}: no queries")
    observed = observed_spreads(args.from_json) if args.from_json else None

    print(render(shape, observed, markdown=False))
    if args.markdown:
        Path(args.markdown).write_text(render(shape, observed, markdown=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
