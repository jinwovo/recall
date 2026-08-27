"""Retrieval eval harness + CI regression gate (docs/adr/0001, 0007, 0011).

Sweeps BM25-only / vector-only / hybrid against a labeled query set and reports doc-level
Recall@5, Recall@10, MRR@10 and nDCG@10 — the hybrid lift is the README headline. With
--gate it enforces a policy on one mode and exits non-zero on a regression, which is what
CI runs on every PR.

Rankings come back chunk-level; metrics are doc-level, so ranked docIds are deduplicated by
first occurrence before scoring (otherwise a doc with several matching chunks occupies
several ranks and DCG can exceed the ideal DCG).

Every score is an estimate from a finite query sample, so every score is reported with a
95% interval (exact binomial where the per-query metric is 0/1, BCa bootstrap otherwise),
and every mode is compared against the baseline with a paired randomization test whose
p-values are Holm-corrected across the sweep (docs/adr/0011). The report also states what
the gold set can resolve at all — on a small set that is usually the finding.

Four gate policies:

    point       measured >= threshold                     (default; absolute floor)
    ci-lower    lower bound of the 95% interval >= threshold
    regression  paired randomization test against --baseline-json; fails only on a
                statistically significant drop on the same queries
    sequential  score queries one at a time and stop the moment the verdict is settled,
                using an anytime-valid confidence sequence (docs/adr/0012)

`regression` is the sensitive one: it compares per-query scores against a recorded run
rather than an absolute line, so a real two-query regression is caught even when the mean
still clears the threshold.

`sequential` is the cheap one: a system comfortably above the line is proven so in a
fraction of the gold set, and a broken one fails in fewer queries still. Stopping early is
legitimate here — and only here — because the interval it stops on is valid at every
sample size rather than only at the last one.

Usage:
    python run_eval.py gold.jsonl
    python run_eval.py gold.jsonl --json out.json --markdown summary.md
    python run_eval.py gold.jsonl --gate                              # absolute thresholds
    python run_eval.py gold.jsonl --gate --gate-policy regression \\
        --baseline-json baseline.json                                 # paired regression
    python run_eval.py gold.jsonl --gate --gate-policy sequential     # stop when decided
Env:
    RECALL_API (default http://localhost:8080)

Stdlib only — no dependencies to install in CI. Ships `stats.py` and `sequential.py`
beside it.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import sequential
import stats

# Keep console output alive on narrow encodings (e.g. Windows cp949) — degrade, don't crash.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
DEFAULT_MODES = ["bm25", "vector", "hybrid"]      # what CI sweeps — deterministic, no LLM
# Experimental modes (docs/adr/0008) opt in via --modes; each maps to API query params.
MODE_PARAMS = {
    "bm25": "mode=bm25",
    "vector": "mode=vector",
    "hybrid": "mode=hybrid",
    "hybrid-m3": "mode=hybrid&rerank=m3",         # bge-m3 tri-modal self-hybrid rerank
    "hyde": "mode=hyde",                          # LLM hypothetical-document embeddings
}
RETRIEVE_K = 10          # doc-level ranking depth; Recall is also reported at the 5 cutoff
RECALL_CUTOFFS = (5, 10)

METRIC_COLUMNS = ["recall@5", "recall@10", "mrr@10", "ndcg@10"]
# Aggregate metric -> the per-query key it averages, so intervals and paired tests can be
# built from the same per-query records the report already carries.
METRIC_TO_QUERY_KEY = {
    "recall@5": "recall@5",
    "recall@10": "recall@10",
    "mrr@10": "rr",
    "ndcg@10": "ndcg",
}
ALPHA = 0.05
# What counts as a retrieval improvement worth shipping — used only for the sample-size
# advice in the design-analysis block, never as a gate.
TARGET_EFFECT = 0.02
# Fixed so a sequential gate reads the gold set in the same order on every run: the shuffle
# is there to make the prefix representative, not to add variance between builds.
SEQUENTIAL_SEED = 1337
# A badly broken run misses almost every query; listing them all buries the verdict.
MAX_LISTED_MISSES = 15


def search(query: str, mode: str, attempts: int = 3) -> list[str]:
    """Ranked docIds for a query, deduplicated by first occurrence (chunk → doc level)."""
    url = f"{API}/api/search?q=" + urllib.parse.quote(query) + f"&{MODE_PARAMS[mode]}"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = json.load(r)
            seen: dict[str, None] = {}
            for chunk in data.get("results", []):
                seen.setdefault(chunk["docId"], None)
            return list(seen)[:RETRIEVE_K]
        except (urllib.error.URLError, TimeoutError) as e:   # transient — retry with backoff
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"search failed after {attempts} attempts: {url}") from last


def score_query(example: dict, mode: str) -> dict:
    """Retrieve for one query and score it — the unit both the batch and sequential paths use."""
    gold = set(example["relevant_doc_ids"])
    ranked = search(example["query"], mode)

    recalls = {}
    for k in RECALL_CUTOFFS:
        hits = sum(1 for d in ranked[:k] if d in gold)
        recalls[k] = hits / len(gold) if gold else 0.0

    first_hit = next((i + 1 for i, d in enumerate(ranked) if d in gold), None)
    rr = 1.0 / first_hit if first_hit else 0.0

    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked) if d in gold)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), RETRIEVE_K)))
    ndcg = dcg / idcg if idcg else 0.0

    return {
        "query": example["query"],
        "gold": sorted(gold),
        "ranked": ranked,
        "first_hit_rank": first_hit,
        "recall@5": recalls[5],
        "recall@10": recalls[10],
        "rr": rr,
        "ndcg": ndcg,
    }


def aggregate(per_query: list[dict]) -> dict:
    n = len(per_query) or 1
    return {
        "recall@5": sum(q["recall@5"] for q in per_query) / n,
        "recall@10": sum(q["recall@10"] for q in per_query) / n,
        "mrr@10": sum(q["rr"] for q in per_query) / n,
        "ndcg@10": sum(q["ndcg"] for q in per_query) / n,
        "per_query": per_query,
    }


def evaluate_mode(examples: list[dict], mode: str) -> dict:
    return aggregate([score_query(ex, mode) for ex in examples])


def evaluate_sequential(examples: list[dict], mode: str, thresholds: dict[str, float],
                        alpha: float, seed: int) -> tuple[dict, dict]:
    """Retrieve query by query and stop as soon as the gate's verdict is settled.

    This is where the saving is actually taken: the loop exits before the remaining
    queries are ever sent, so the unspent budget is unspent HTTP requests, LLM-judge
    calls and wall-clock — not a smaller number in a report.

    Two exits. All metrics decided is the normal one. *Any* metric deciding `fail` is the
    other, and it short-circuits: the build is already red, and confirming the other two
    metrics costs queries to learn nothing that changes the outcome.

    The query order is a seeded shuffle. A gold set arrives grouped by topic, and a
    sequential procedure reading it in file order would be deciding about whatever the
    first section happens to contain.
    """
    ordered = sequential.shuffled(examples, seed)
    gates = {metric: sequential.SequentialGate(threshold, metric, alpha)
             for metric, threshold in thresholds.items()}
    per_query: list[dict] = []
    stop_reason = "budget exhausted"

    for example in ordered:
        scored = score_query(example, mode)
        per_query.append(scored)
        for metric, gate in gates.items():
            gate.update(scored[METRIC_TO_QUERY_KEY[metric]])
        failed = [m for m, g in gates.items() if g.decision == "fail"]
        if failed:
            stop_reason = f"{failed[0]} failed"
            break
        if all(g.decided for g in gates.values()):
            stop_reason = "all metrics decided"
            break

    verdicts = {
        metric: {
            "metric": metric,
            "threshold": gate.threshold,
            "decision": gate.decision,
            "measured": stats.mean(values_of({"per_query": per_query}, metric)),
            "pass": gate.decision == "pass",
        } for metric, gate in gates.items()
    }
    summary = {
        "queries_used": len(per_query),
        "queries_available": len(ordered),
        "saved": len(ordered) - len(per_query),
        "saved_fraction": (len(ordered) - len(per_query)) / len(ordered) if ordered else 0.0,
        "stopped_early": stop_reason != "budget exhausted",
        "stop_reason": stop_reason,
        "alpha": alpha,
        "seed": seed,
    }
    return aggregate(per_query), {"verdicts": verdicts, "summary": summary}


# --------------------------------------------------------------------------------------
# inference over the per-query records (docs/adr/0011)
# --------------------------------------------------------------------------------------

def values_of(result: dict, metric: str) -> list[float]:
    key = METRIC_TO_QUERY_KEY[metric]
    return [float(q[key]) for q in result["per_query"]]


def interval(values: list[float], iters: int) -> dict:
    """95% interval for a per-query mean, by the method the metric's support allows.

    A metric that is 0/1 on every query — Recall@k when each query has one relevant
    document — is a binomial proportion, and the exact interval is both correct at the
    0/n and n/n boundaries and tighter in reasoning than a bootstrap that cannot resample
    a value it never saw. Everything else gets BCa.
    """
    if values and all(v in (0.0, 1.0) for v in values):
        successes = sum(1 for v in values if v == 1.0)
        lo, hi = stats.clopper_pearson(successes, len(values), ALPHA)
        return {"lo": lo, "hi": hi, "method": "exact-binomial",
                "successes": successes, "n": len(values)}
    lo, hi = stats.bootstrap_ci(values, ALPHA, iters)
    return {"lo": lo, "hi": hi, "method": "bca-bootstrap", "n": len(values)}


def add_intervals(results: dict[str, dict], iters: int) -> None:
    for result in results.values():
        result["ci"] = {m: interval(values_of(result, m), iters) for m in METRIC_COLUMNS}


def compare_modes(results: dict[str, dict], baseline: str, iters: int) -> dict:
    """Paired randomization test of every other mode against the baseline, Holm-corrected.

    Correction is applied within each metric across the modes compared: sweeping four
    challengers on nDCG@10 is four chances to see a win, and an uncorrected 0.05 is not
    an 0.05.
    """
    challengers = [m for m in results if m != baseline]
    comparisons: dict[str, list[dict]] = {}
    for metric in METRIC_COLUMNS:
        base_values = values_of(results[baseline], metric)
        rows = []
        for mode in challengers:
            test = stats.paired_permutation_test(
                values_of(results[mode], metric), base_values, iters=iters)
            rows.append({"mode": mode, **test.as_dict()})
        for row, adjusted in zip(rows, stats.holm([r["p_value"] for r in rows])):
            row["holm_p"] = adjusted
            row["significant"] = adjusted < ALPHA and not row["underpowered"]
        comparisons[metric] = rows
    return {"baseline": baseline, "metrics": comparisons}


def verdict_of(row: dict) -> str:
    if row["underpowered"]:
        return (f"unresolvable ({row['effective_n']}/{row['n_pairs']} queries differ; "
                f"floor p = {row['min_attainable_p']:.3f})")
    if row["significant"]:
        return "significant"
    return "not significant"


def design_of(results: dict[str, dict], mode: str, baseline: str, metric: str) -> dict:
    d = stats.design_analysis(values_of(results[mode], metric),
                              values_of(results[baseline], metric),
                              alpha=ALPHA, target_effect=TARGET_EFFECT)
    return {"metric": metric, "mode": mode, "baseline": baseline, **d.as_dict()}


# --------------------------------------------------------------------------------------
# gate policies
# --------------------------------------------------------------------------------------

def gate_point(result: dict, thresholds: dict[str, float]) -> list[dict]:
    return [{
        "metric": metric,
        "measured": round(result[metric], 4),
        "threshold": minimum,
        "pass": result[metric] >= minimum,
        "detail": "point estimate",
    } for metric, minimum in thresholds.items()]


def gate_ci_lower(result: dict, thresholds: dict[str, float]) -> list[dict]:
    checks = []
    for metric, minimum in thresholds.items():
        lo = result["ci"][metric]["lo"]
        checks.append({
            "metric": metric,
            "measured": round(result[metric], 4),
            "ci_lower": round(lo, 4),
            "threshold": minimum,
            "pass": lo >= minimum,
            "detail": f"95% lower bound ({result['ci'][metric]['method']})",
        })
    return checks


def gate_sequential(run: dict) -> list[dict]:
    """Turn the sequential verdicts into gate rows.

    `undecided` fails. The budget ran out before the evidence arrived, which is a fact
    about the gold set rather than a clean bill of health — and a gate that treats "we
    could not tell" as a pass has quietly stopped gating.
    """
    summary = run["summary"]
    used, available = summary["queries_used"], summary["queries_available"]

    def detail(verdict: dict) -> str:
        if verdict["decision"] != "undecided":
            return f"{verdict['decision']} after {used}/{available} queries"
        if summary["stop_reason"] == "budget exhausted":
            return f"undecided after all {available} queries — gold set too small to tell"
        # Short-circuited: the build was already red, so this metric was never settled.
        return f"not settled — stopped at {used}/{available} once {summary['stop_reason']}"

    return [{
        "metric": v["metric"],
        "measured": round(v["measured"], 4),
        "threshold": v["threshold"],
        "pass": v["pass"],
        "detail": detail(v),
    } for v in run["verdicts"].values()]


def gate_regression(result: dict, baseline_result: dict, thresholds: dict[str, float],
                    iters: int) -> list[dict]:
    """Fail only on a statistically significant paired drop against a recorded run.

    Absolute thresholds are still applied — a gate should notice a collapse even with no
    baseline to compare against — but the sensitive half is the paired test: the same
    queries, scored before and after, so a regression that leaves the mean above the line
    is still caught.
    """
    checks = gate_point(result, thresholds)
    per_metric = {row["metric"]: row for row in checks}
    for metric in thresholds:
        current = values_of(result, metric)
        previous = values_of(baseline_result, metric)
        row = per_metric[metric]
        if len(current) != len(previous):
            row["detail"] = (f"baseline has {len(previous)} queries, this run has "
                             f"{len(current)} — paired test skipped")
            continue
        test = stats.paired_permutation_test(current, previous, iters=iters)
        regressed = test.observed_diff < 0 and test.p_value < ALPHA
        row.update({
            "baseline_measured": round(stats.mean(previous), 4),
            "delta": round(test.observed_diff, 4),
            "p_value": round(test.p_value, 4),
            "effective_n": test.effective_n,
            "pass": row["pass"] and not regressed,
            "detail": ("significant drop vs baseline" if regressed
                       else f"no significant drop (p = {test.p_value:.3f})"),
        })
    return checks


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------

def print_report(results: dict[str, dict], queries: int, comparison: dict | None,
                 design: dict | None) -> None:
    print(f"queries={queries}  ranking depth={RETRIEVE_K} (doc-level)  "
          f"intervals={int((1 - ALPHA) * 100)}%\n")
    header = f"{'mode':<10}" + "".join(f"{c:>22}" for c in METRIC_COLUMNS)
    print(header)
    print("-" * len(header))
    for mode, r in results.items():
        cells = ""
        for c in METRIC_COLUMNS:
            ci = r["ci"][c]
            cells += f"{r[c]:>9.3f} [{ci['lo']:.2f},{ci['hi']:.2f}]"
        print(f"{mode:<10}" + cells)
    print()

    if comparison:
        base = comparison["baseline"]
        print(f"paired randomization test vs {base} (Holm-corrected within each metric)")
        for metric, rows in comparison["metrics"].items():
            for row in rows:
                print(f"  {metric:<10} {row['mode']:<10} "
                      f"delta={row['observed_diff']:+.3f} p={row['p_value']:.4f} "
                      f"holm={row['holm_p']:.4f}  {verdict_of(row)}")
        print()

    if design:
        print(f"resolution of this gold set ({design['mode']} vs {design['baseline']}, "
              f"{design['metric']}):")
        print(f"  n = {design['n']} queries, per-query difference sd = "
              f"{design['sd_of_differences']:.3f}")
        print(f"  smallest detectable effect at 80% power: "
              f"{design['min_detectable_effect']:+.3f}")
        if design["queries_for_target"]:
            print(f"  queries needed to resolve {design['target_effect']:+.3f}: "
                  f"{design['queries_for_target']:,}")
        print()

    for mode, r in results.items():
        misses = [q for q in r["per_query"] if q["first_hit_rank"] != 1]
        for q in misses[:MAX_LISTED_MISSES]:
            print(f"  [{mode}] first hit @{q['first_hit_rank'] or 'miss'}: {q['query']}")
        if len(misses) > MAX_LISTED_MISSES:
            print(f"  [{mode}] ... and {len(misses) - MAX_LISTED_MISSES} more "
                  f"(full list in --json)")


def render_markdown(results: dict[str, dict], queries: int, gate: list[dict] | None,
                    gate_mode: str, gate_policy: str, comparison: dict | None,
                    design: dict | None, sequential_run: dict | None = None) -> str:
    modes = list(results)
    lines = ["## Retrieval eval — " + " vs ".join(modes), "",
             f"{queries} queries · doc-level ranking depth {RETRIEVE_K} · `{API}`", "",
             f"Point estimate with its {int((1 - ALPHA) * 100)}% interval — exact binomial "
             "where the per-query metric is 0/1, BCa bootstrap otherwise "
             "([ADR 0011](docs/adr/0011-statistical-inference-eval.md)).", "",
             "| mode | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |",
             "|---|:---:|:---:|:---:|:---:|"]
    for mode, r in results.items():
        cells = []
        for c in METRIC_COLUMNS:
            ci = r["ci"][c]
            cells.append(f"{r[c]:.3f}<br><sub>[{ci['lo']:.2f}, {ci['hi']:.2f}]</sub>")
        label = f"**{mode}**" if mode == gate_mode and gate is not None else mode
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    if comparison:
        base = comparison["baseline"]
        lines += [f"### Significance vs `{base}`", "",
                  "Paired randomization test on per-query scores, Holm-corrected across the "
                  "modes compared within each metric. `unresolvable` means the queries that "
                  "differ are too few for *any* outcome to reach p < 0.05 — a sample-size "
                  "fact, not a result.", "",
                  "| metric | mode | Δ | p | Holm p | verdict |",
                  "|---|---|:---:|:---:|:---:|---|"]
        for metric, rows in comparison["metrics"].items():
            for row in rows:
                mark = "✅ " if row["significant"] else ""
                lines.append(f"| {metric} | `{row['mode']}` | {row['observed_diff']:+.3f} "
                             f"| {row['p_value']:.4f} | {row['holm_p']:.4f} "
                             f"| {mark}{verdict_of(row)} |")
        lines.append("")

    if design:
        lines += ["### What this gold set can resolve", "",
                  f"`{design['mode']}` vs `{design['baseline']}` on **{design['metric']}**, "
                  f"n = {design['n']} queries, per-query difference sd = "
                  f"{design['sd_of_differences']:.3f}.", "",
                  f"- Smallest effect detectable at 80% power: "
                  f"**{design['min_detectable_effect']:+.3f}**",
                  f"- Smallest two-sided p this comparison can return: "
                  f"**{design['min_attainable_p']:.4f}**"]
        if design["queries_for_target"]:
            lines.append(f"- Queries needed to resolve a {design['target_effect']:+.3f} "
                         f"change: **{design['queries_for_target']:,}**")
        lines.append("")

    if sequential_run:
        s = sequential_run["summary"]
        lines += ["### Stopped early", "",
                  f"Scored **{s['queries_used']} of {s['queries_available']}** queries and "
                  f"stopped once the verdict was settled — **{s['saved_fraction']:.0%}** of "
                  f"the query budget left unspent. The interval this stopped on is valid at "
                  f"every sample size ([ADR 0012](docs/adr/0012-anytime-valid-evaluation.md)), "
                  f"which is what makes stopping early a decision rather than a peek.", "",
                  f"α = {s['alpha']}, shuffle seed = {s['seed']}.", ""]

    if gate is not None:
        verdict = "✅ **PASS**" if all(c["pass"] for c in gate) else "❌ **FAIL**"
        lines += [f"### Regression gate ({gate_mode}, policy `{gate_policy}`): {verdict}", "",
                  "| metric | measured | threshold | detail | |",
                  "|---|:---:|:---:|---|:---:|"]
        for c in gate:
            measured = f"{c['measured']:.3f}"
            if "ci_lower" in c:
                measured += f" <sub>(lo {c['ci_lower']:.3f})</sub>"
            if "delta" in c:
                measured += f" <sub>({c['delta']:+.3f})</sub>"
            lines.append(f"| {c['metric']} | {measured} | ≥ {c['threshold']:.2f} "
                         f"| {c['detail']} | {'✅' if c['pass'] else '❌'} |")
        lines.append("")

    lines += ["<details><summary>First relevant rank per query</summary>", "",
              "| query | " + " | ".join(modes) + " |",
              "|---|" + ":---:|" * len(modes)]
    for i, ex in enumerate(results[modes[0]]["per_query"]):
        ranks = [str(results[m]["per_query"][i]["first_hit_rank"] or "—") for m in modes]
        query = ex["query"] if len(ex["query"]) <= 48 else ex["query"][:47] + "…"
        lines.append(f"| {query} | " + " | ".join(ranks) + " |")
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="queries.example.jsonl",
                        help="JSONL with {query, relevant_doc_ids} per line")
    parser.add_argument("--json", metavar="PATH", help="write full results as JSON")
    parser.add_argument("--markdown", metavar="PATH",
                        help="write a markdown report (GitHub step summary friendly)")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES),
                        help="comma-separated modes to sweep; also: "
                             + ", ".join(m for m in MODE_PARAMS if m not in DEFAULT_MODES))
    parser.add_argument("--baseline-mode", default=None,
                        help="mode the significance tests compare against "
                             "(default: the first swept mode)")
    parser.add_argument("--bootstrap-iters", type=int, default=stats.DEFAULT_ITERS,
                        help="resampling draws for intervals and Monte Carlo p-values")
    parser.add_argument("--no-stats", action="store_true",
                        help="skip intervals and significance tests (point estimates only)")
    parser.add_argument("--gate", action="store_true",
                        help="enforce the gate policy on --gate-mode; exit 1 on regression")
    parser.add_argument("--gate-mode", default="hybrid", choices=list(MODE_PARAMS))
    parser.add_argument("--gate-policy", default="point",
                        choices=["point", "ci-lower", "regression", "sequential"],
                        help="point: measured >= threshold. ci-lower: 95%% lower bound >= "
                             "threshold. regression: also fail on a significant paired drop "
                             "against --baseline-json. sequential: stop as soon as the "
                             "verdict is settled (anytime-valid)")
    parser.add_argument("--baseline-json", metavar="PATH",
                        help="a previous --json result; required by --gate-policy regression")
    parser.add_argument("--sequential-seed", type=int, default=SEQUENTIAL_SEED,
                        help="seed for the query shuffle a sequential gate reads in")
    parser.add_argument("--min-recall5", type=float, default=0.90)
    parser.add_argument("--min-mrr10", type=float, default=0.85)
    parser.add_argument("--min-ndcg10", type=float, default=0.85)
    args = parser.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODE_PARAMS]
    if unknown:
        parser.error(f"unknown mode(s) {unknown}; available: {list(MODE_PARAMS)}")
    if args.gate and args.gate_mode not in modes:
        parser.error(f"--gate-mode {args.gate_mode} is not in --modes {modes}")
    baseline_mode = args.baseline_mode or modes[0]
    if baseline_mode not in modes:
        parser.error(f"--baseline-mode {baseline_mode} is not in --modes {modes}")
    if args.gate_policy == "regression" and not args.baseline_json:
        parser.error("--gate-policy regression requires --baseline-json")

    thresholds = {"recall@5": args.min_recall5, "mrr@10": args.min_mrr10,
                  "ndcg@10": args.min_ndcg10}

    # A sequential gate answers one question as cheaply as possible; sweeping other modes
    # to fill a comparison table would spend exactly the queries it exists to save.
    sequential_run = None
    if args.gate and args.gate_policy == "sequential":
        if modes != [args.gate_mode]:
            print(f"note: sequential gating evaluates only '{args.gate_mode}' — "
                  f"skipping {[m for m in modes if m != args.gate_mode]}\n")
        modes = [args.gate_mode]
        gate_result, sequential_run = evaluate_sequential(
            examples, args.gate_mode, thresholds, ALPHA, args.sequential_seed)
        results = {args.gate_mode: gate_result}
        baseline_mode = args.gate_mode
    else:
        results = {mode: evaluate_mode(examples, mode) for mode in modes}

    comparison = design = None
    if not args.no_stats:
        add_intervals(results, args.bootstrap_iters)
        if len(modes) > 1:
            comparison = compare_modes(results, baseline_mode, args.bootstrap_iters)
            design = design_of(results, args.gate_mode if args.gate else modes[-1],
                               baseline_mode, "mrr@10")

    if args.no_stats:
        # Keep the printer's contract without paying for resampling.
        for r in results.values():
            r["ci"] = {m: {"lo": r[m], "hi": r[m], "method": "none"} for m in METRIC_COLUMNS}
    print_report(results, len(examples), comparison, design)

    if sequential_run:
        s = sequential_run["summary"]
        print(f"sequential gate: scored {s['queries_used']}/{s['queries_available']} "
              f"queries, {s['saved_fraction']:.0%} of the budget unspent "
              f"({s['stop_reason']}; alpha={s['alpha']}, seed={s['seed']})")
        for v in sequential_run["verdicts"].values():
            print(f"  {v['metric']:<10} {v['decision']:<10} "
                  f"measured={v['measured']:.3f} threshold={v['threshold']:.2f}")
        print()

    gate = None
    if args.gate:
        if args.gate_policy == "sequential":
            gate = gate_sequential(sequential_run)
        elif args.gate_policy == "point":
            gate = gate_point(results[args.gate_mode], thresholds)
        elif args.gate_policy == "ci-lower":
            gate = gate_ci_lower(results[args.gate_mode], thresholds)
        else:
            with open(args.baseline_json, encoding="utf-8") as f:
                baseline_run = json.load(f)
            if args.gate_mode not in baseline_run.get("modes", {}):
                parser.error(f"--baseline-json has no '{args.gate_mode}' mode to compare against")
            gate = gate_regression(results[args.gate_mode],
                                   baseline_run["modes"][args.gate_mode],
                                   thresholds, args.bootstrap_iters)
        for c in gate:
            print(f"gate[{args.gate_mode}/{args.gate_policy}] {c['metric']}: "
                  f"{c['measured']:.3f} >= {c['threshold']:.2f} "
                  f"({c['detail']}) ... {'PASS' if c['pass'] else 'FAIL'}")

    if args.json:
        payload = {
            "api": API,
            "queries": len(examples),
            "retrieved_k": RETRIEVE_K,
            "alpha": ALPHA,
            "modes": results,
            "comparison": comparison,
            "design": design,
            "sequential": sequential_run,
            "gate": None if gate is None else {
                "mode": args.gate_mode,
                "policy": args.gate_policy,
                "checks": gate,
                "pass": all(c["pass"] for c in gate),
            },
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(results, len(examples), gate, args.gate_mode,
                                    args.gate_policy, comparison, design, sequential_run))

    if gate is not None and not all(c["pass"] for c in gate):
        print("\nregression gate FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
