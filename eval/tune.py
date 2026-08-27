"""Self-tuning sweep for hybrid retrieval (docs/adr/0010, docs/adr/0011).

Probes a grid of retrieval knobs — RRF k and fused candidate count — through the public
API's clamped tuning overrides (`/api/search?...&rrfK=&candidates=`), and proposes a new
default only when the improvement survives every way it could be an artifact.

An automated tuner is a machine for finding differences, and most differences on a small
query set are noise. Picking the best of nine configurations on ten queries and shipping
it is not tuning; it is selecting the largest random deviation and writing it into
`application.yml`. Four guards stand between the sweep and a pull request:

1. **Held-out split.** The grid is searched on a `dev` split only. The `test` split is
   touched exactly twice — baseline and winner — and never influences selection.
2. **Effect-size floor.** The dev improvement must exceed `--epsilon`, so a difference too
   small to matter cannot open a PR however clean its statistics.
3. **Evidence on dev.** A paired randomization test against the baseline, **Holm-corrected
   across the whole grid** — nine configurations is nine chances at a false win, and an
   uncorrected 0.05 is not an 0.05.
4. **Generalisation.** The winner must still improve on the held-out split, and must not
   be significantly worse there. The gap between the dev and test improvements is the
   overfitting the search introduced, and it is printed either way.

When the gold set is too small to clear these — which a ten-query set is, by arithmetic
rather than by bad luck — the tuner reports the query count that would be needed and
proposes nothing. A loop that cannot tell an improvement from a coin flip should not be
allowed to open pull requests.

Usage:
    python tune.py gold.jsonl --report tune-report.md --json tune-result.json
    python tune.py gold.jsonl --apply          # rewrite application.yml with the winner
Env:
    RECALL_API (default http://localhost:8080)

Stdlib only.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import stats

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
APPLICATION_YML = Path(__file__).resolve().parent.parent / "backend/src/main/resources/application.yml"
RETRIEVE_K = 10
GRID_RRF_K = (20, 60, 120)
GRID_CANDIDATES = (25, 50, 100)
ALPHA = 0.05
SPLIT_SEED = 20240917          # fixed: the same gold set always splits the same way
HANGUL = re.compile(r"[\uac00-\ud7a3]")


def search(query: str, rrf_k: int, candidates: int, attempts: int = 3) -> list[str]:
    url = (f"{API}/api/search?q=" + urllib.parse.quote(query)
           + f"&mode=hybrid&rrfK={rrf_k}&candidates={candidates}")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = json.load(r)
            seen: dict[str, None] = {}
            for chunk in data.get("results", []):
                seen.setdefault(chunk["docId"], None)
            return list(seen)[:RETRIEVE_K]
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"search failed: {url}") from last


# --------------------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------------------

def stratum_of(example: dict) -> str:
    """Group queries so a split cannot accidentally quarantine a whole query type.

    Language is the axis that matters here: Korean queries over an English corpus are the
    ones BM25 misses and dense retrieval catches, so they carry most of the signal a
    fusion knob moves. A split that put them all on one side would tune on a different
    problem than it validates on.
    """
    return example.get("lang") or ("ko" if HANGUL.search(example["query"]) else "en")


def split_gold(examples: list[dict], dev_fraction: float,
               seed: int = SPLIT_SEED) -> tuple[list[dict], list[dict]]:
    """Deterministic stratified dev/test split — same gold set, same split, every night."""
    import random
    strata: dict[str, list[dict]] = {}
    for ex in examples:
        strata.setdefault(stratum_of(ex), []).append(ex)
    dev: list[dict] = []
    test: list[dict] = []
    for name in sorted(strata):
        group = sorted(strata[name], key=lambda e: e["query"])
        random.Random(f"{seed}:{name}").shuffle(group)
        cut = round(len(group) * dev_fraction)
        dev.extend(group[:cut])
        test.extend(group[cut:])
    return dev, test


# --------------------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------------------

def evaluate(examples: list[dict], rrf_k: int, candidates: int) -> dict:
    """Score one configuration, keeping the per-query values the paired tests need."""
    recalls, rrs, ndcgs = [], [], []
    for ex in examples:
        gold = set(ex["relevant_doc_ids"])
        ranked = search(ex["query"], rrf_k, candidates)
        hits5 = sum(1 for d in ranked[:5] if d in gold)
        recalls.append(hits5 / len(gold) if gold else 0.0)
        rrs.append(next((1.0 / (i + 1) for i, d in enumerate(ranked) if d in gold), 0.0))
        dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked) if d in gold)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), RETRIEVE_K)))
        ndcgs.append(dcg / idcg if idcg else 0.0)
    return {"rrf_k": rrf_k, "candidates": candidates,
            "recall5": stats.mean(recalls), "mrr10": stats.mean(rrs),
            "ndcg10": stats.mean(ndcgs),
            "per_query": {"recall5": recalls, "rr": rrs, "ndcg": ndcgs}}


def baseline_config() -> tuple[int, int]:
    text = APPLICATION_YML.read_text(encoding="utf-8")
    rrf_k = int(re.search(r"^\s*rrf-k:\s*(\d+)", text, re.M).group(1))
    candidates = int(re.search(r"^\s*candidates:\s*(\d+)", text, re.M).group(1))
    return rrf_k, candidates


def apply_config(rrf_k: int, candidates: int) -> None:
    """Rewrite only the numbers; comments and layout stay put."""
    text = APPLICATION_YML.read_text(encoding="utf-8")
    text = re.sub(r"^(\s*rrf-k:\s*)\d+", rf"\g<1>{rrf_k}", text, count=1, flags=re.M)
    text = re.sub(r"^(\s*candidates:\s*)\d+", rf"\g<1>{candidates}", text, count=1, flags=re.M)
    APPLICATION_YML.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------------------

def label(config: dict) -> str:
    return f"rrf-k={config['rrf_k']} candidates={config['candidates']}"


def decide(dev_results: list[dict], dev_base: dict, dev_best: dict,
           test_base: dict | None, test_best: dict | None, epsilon: float) -> dict:
    """Run the four guards in order and record what each one said.

    Every guard is evaluated even after one fails, because a report that only shows the
    first failure hides whether the others would have passed — and that is the information
    a human needs to decide whether the gold set or the change is the problem.
    """
    dev_delta = dev_best["mrr10"] - dev_base["mrr10"]

    # Guard 3 is computed first: its Holm correction needs the whole grid's p-values.
    grid_tests = [
        stats.paired_permutation_test(r["per_query"]["rr"], dev_base["per_query"]["rr"])
        for r in dev_results
    ]
    adjusted = stats.holm([t.p_value for t in grid_tests])
    by_config = {(r["rrf_k"], r["candidates"]): (t, p)
                 for r, t, p in zip(dev_results, grid_tests, adjusted)}
    dev_test, dev_holm_p = by_config[(dev_best["rrf_k"], dev_best["candidates"])]

    guards = [
        {"name": "effect size",
         "pass": dev_delta >= epsilon,
         "detail": f"dev ΔMRR@10 = {dev_delta:+.4f}, floor = {epsilon:+.4f}"},
        {"name": "evidence on dev",
         "pass": dev_holm_p < ALPHA and not dev_test.underpowered,
         "detail": (f"paired p = {dev_test.p_value:.4f}, Holm p = {dev_holm_p:.4f} "
                    f"across {len(dev_results)} configurations; "
                    f"{dev_test.effective_n}/{dev_test.n_pairs} dev queries differ "
                    f"(floor p = {dev_test.min_attainable_p:.4f})")},
    ]

    generalisation = {"name": "generalisation", "pass": False, "detail": "held-out split not evaluated"}
    held_out = None
    if test_base is not None and test_best is not None:
        test_delta = test_best["mrr10"] - test_base["mrr10"]
        confirm = stats.paired_permutation_test(test_best["per_query"]["rr"],
                                                test_base["per_query"]["rr"])
        significantly_worse = test_delta < 0 and confirm.p_value < ALPHA
        generalisation = {
            "name": "generalisation",
            "pass": test_delta > 0 and not significantly_worse,
            "detail": (f"held-out ΔMRR@10 = {test_delta:+.4f} "
                       f"(p = {confirm.p_value:.4f}); overfitting gap "
                       f"{dev_delta - test_delta:+.4f}"),
        }
        held_out = {"delta": test_delta, "gap": dev_delta - test_delta,
                    **confirm.as_dict()}
    guards.append(generalisation)

    # Not a guard — the reason a failing run failed, and the number that would fix it.
    design = stats.design_analysis(dev_best["per_query"]["rr"], dev_base["per_query"]["rr"],
                                   alpha=ALPHA, target_effect=epsilon)

    return {
        "improved": all(g["pass"] for g in guards),
        "guards": guards,
        "dev_delta": dev_delta,
        "dev_p": dev_test.p_value,
        "dev_holm_p": dev_holm_p,
        "dev_effective_n": dev_test.effective_n,
        "held_out": held_out,
        "design": design.as_dict(),
        "grid_p": {label(r): {"p_value": t.p_value, "holm_p": p, "underpowered": t.underpowered}
                   for r, t, p in zip(dev_results, grid_tests, adjusted)},
    }


def render_report(dev_results: list[dict], dev_base: dict, dev_best: dict,
                  decision: dict, n_dev: int, n_test: int) -> str:
    lines = ["## Retrieval self-tuning sweep", "",
             f"Grid over `rrf-k` × `candidates`, hybrid mode, {len(dev_results)} "
             f"configurations · `{API}`", "",
             f"Searched on a **{n_dev}-query dev split**; the **{n_test}-query held-out "
             f"split** was touched only to check the winner "
             f"([ADR 0011](docs/adr/0011-statistical-inference-eval.md)).", "",
             "| `rrf-k` | `candidates` | `Recall@5` | `MRR@10` | `nDCG@10` | Holm p | |",
             "|:---:|:---:|:---:|:---:|:---:|:---:|---|"]
    for r in sorted(dev_results, key=lambda x: (-x["mrr10"], -x["ndcg10"])):
        tag = []
        if (r["rrf_k"], r["candidates"]) == (dev_best["rrf_k"], dev_best["candidates"]):
            tag.append("**dev winner**")
        if (r["rrf_k"], r["candidates"]) == (dev_base["rrf_k"], dev_base["candidates"]):
            tag.append("baseline")
        holm = decision["grid_p"][label(r)]["holm_p"]
        lines.append(f"| {r['rrf_k']} | {r['candidates']} | {r['recall5']:.3f} "
                     f"| {r['mrr10']:.3f} | {r['ndcg10']:.3f} | {holm:.3f} | {' '.join(tag)} |")
    lines += ["", "### Guards", "", "| guard | verdict | detail |", "|---|:---:|---|"]
    for g in decision["guards"]:
        lines.append(f"| {g['name']} | {'✅' if g['pass'] else '❌'} | {g['detail']} |")
    lines.append("")

    if decision["improved"]:
        lines.append(f"All guards pass — proposing `rrf-k: {dev_best['rrf_k']}`, "
                     f"`candidates: {dev_best['candidates']}`. The eval gate on this PR "
                     f"has to confirm it before a human merges.")
    else:
        failed = [g["name"] for g in decision["guards"] if not g["pass"]]
        lines.append(f"**No proposal** — failed: {', '.join(failed)}. Current defaults stand.")
        design = decision["design"]
        if design.get("queries_for_target"):
            lines += ["", f"The dev split's per-query spread is "
                          f"{design['sd_of_differences']:.3f}, so resolving a "
                          f"{design['target_effect']:+.3f} MRR@10 change at 80% power needs "
                          f"**{design['queries_for_target']:,} queries** — against "
                          f"{design['n']} in this split. Until the gold set reaches that "
                          f"scale the sweep is measuring run-to-run variation, and the "
                          f"right change is a bigger benchmark, not a smaller epsilon."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="gold.jsonl")
    parser.add_argument("--epsilon", type=float, default=0.01,
                        help="minimum dev MRR@10 lift that counts as an improvement at all")
    parser.add_argument("--dev-fraction", type=float, default=0.5,
                        help="share of the gold set the grid is searched on")
    parser.add_argument("--split-seed", type=int, default=SPLIT_SEED)
    parser.add_argument("--report", metavar="PATH", help="write a markdown report")
    parser.add_argument("--json", metavar="PATH", help="write machine-readable result")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite application.yml defaults with the winner (if proposed)")
    args = parser.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]
    if len(examples) < 4:
        raise SystemExit(f"{args.gold}: {len(examples)} queries is too few to split")

    dev, test = split_gold(examples, args.dev_fraction, args.split_seed)
    print(f"gold={len(examples)}  dev={len(dev)}  held-out={len(test)}  "
          f"(stratified, seed={args.split_seed})\n")

    base_rrf_k, base_candidates = baseline_config()
    combos = sorted({(k, c) for k in GRID_RRF_K for c in GRID_CANDIDATES}
                    | {(base_rrf_k, base_candidates)})

    dev_results = []
    for rrf_k, candidates in combos:
        r = evaluate(dev, rrf_k, candidates)
        dev_results.append(r)
        print(f"dev  rrf-k={rrf_k:<4} candidates={candidates:<4} "
              f"recall@5={r['recall5']:.3f} mrr@10={r['mrr10']:.3f} ndcg@10={r['ndcg10']:.3f}")

    dev_base = next(r for r in dev_results
                    if (r["rrf_k"], r["candidates"]) == (base_rrf_k, base_candidates))
    dev_best = max(dev_results, key=lambda r: (r["mrr10"], r["ndcg10"], r["recall5"]))

    # The held-out split is measured only for the baseline and the dev winner, and only
    # after selection is finished — that is what makes it held out.
    test_base = test_best = None
    if test and (dev_best["rrf_k"], dev_best["candidates"]) != (base_rrf_k, base_candidates):
        print(f"\nheld-out check: baseline vs {label(dev_best)}")
        test_base = evaluate(test, base_rrf_k, base_candidates)
        test_best = evaluate(test, dev_best["rrf_k"], dev_best["candidates"])
        print(f"  baseline mrr@10={test_base['mrr10']:.3f}  "
              f"winner mrr@10={test_best['mrr10']:.3f}")

    decision = decide(dev_results, dev_base, dev_best, test_base, test_best, args.epsilon)

    print(f"\nbaseline {label(dev_base)} dev mrr@10={dev_base['mrr10']:.3f}")
    print(f"dev winner {label(dev_best)} dev mrr@10={dev_best['mrr10']:.3f}\n")
    for g in decision["guards"]:
        print(f"  [{'PASS' if g['pass'] else 'FAIL'}] {g['name']}: {g['detail']}")
    print("\n" + ("IMPROVED -> proposing " + label(dev_best) if decision["improved"]
                  else "NO_CHANGE -> defaults stand"))

    if args.report:
        Path(args.report).write_text(
            render_report(dev_results, dev_base, dev_best, decision, len(dev), len(test)),
            encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "improved": decision["improved"],
            "epsilon": args.epsilon,
            "split": {"dev": len(dev), "held_out": len(test), "seed": args.split_seed},
            "baseline": {k: v for k, v in dev_base.items() if k != "per_query"},
            "best": {k: v for k, v in dev_best.items() if k != "per_query"},
            "guards": decision["guards"],
            "held_out_check": decision["held_out"],
            "design": decision["design"],
        }, indent=2), encoding="utf-8")
    if args.apply and decision["improved"]:
        apply_config(dev_best["rrf_k"], dev_best["candidates"])
        print(f"applied to {APPLICATION_YML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
