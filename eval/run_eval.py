"""Retrieval eval harness + CI regression gate (docs/adr/0001, docs/adr/0007).

Sweeps BM25-only / vector-only / hybrid against a labeled query set and reports doc-level
Recall@5, Recall@10, MRR@10 and nDCG@10 — the hybrid lift is the README headline. With
--gate it enforces minimum thresholds on one mode and exits non-zero on a regression, which
is what CI runs on every PR.

Rankings come back chunk-level; metrics are doc-level, so ranked docIds are deduplicated by
first occurrence before scoring (otherwise a doc with several matching chunks occupies
several ranks and DCG can exceed the ideal DCG).

Usage:
    python run_eval.py gold.jsonl
    python run_eval.py gold.jsonl --json out.json --markdown summary.md
    python run_eval.py gold.jsonl --gate            # enforce thresholds, exit 1 on breach
Env:
    RECALL_API (default http://localhost:8080)

Stdlib only — no dependencies to install in CI.
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

# Keep console output alive on narrow encodings (e.g. Windows cp949) — degrade, don't crash.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
MODES = ["bm25", "vector", "hybrid"]
RETRIEVE_K = 10          # doc-level ranking depth; Recall is also reported at the 5 cutoff
RECALL_CUTOFFS = (5, 10)


def search(query: str, mode: str, attempts: int = 3) -> list[str]:
    """Ranked docIds for a query, deduplicated by first occurrence (chunk → doc level)."""
    url = f"{API}/api/search?q=" + urllib.parse.quote(query) + f"&mode={mode}"
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


def evaluate_mode(examples: list[dict], mode: str) -> dict:
    per_query = []
    for ex in examples:
        gold = set(ex["relevant_doc_ids"])
        ranked = search(ex["query"], mode)

        recalls = {}
        for k in RECALL_CUTOFFS:
            hits = sum(1 for d in ranked[:k] if d in gold)
            recalls[k] = hits / len(gold) if gold else 0.0

        first_hit = next((i + 1 for i, d in enumerate(ranked) if d in gold), None)
        rr = 1.0 / first_hit if first_hit else 0.0

        dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked) if d in gold)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), RETRIEVE_K)))
        ndcg = dcg / idcg if idcg else 0.0

        per_query.append({
            "query": ex["query"],
            "gold": sorted(gold),
            "ranked": ranked,
            "first_hit_rank": first_hit,
            "recall@5": recalls[5],
            "recall@10": recalls[10],
            "rr": rr,
            "ndcg": ndcg,
        })

    n = len(per_query) or 1
    return {
        "recall@5": sum(q["recall@5"] for q in per_query) / n,
        "recall@10": sum(q["recall@10"] for q in per_query) / n,
        "mrr@10": sum(q["rr"] for q in per_query) / n,
        "ndcg@10": sum(q["ndcg"] for q in per_query) / n,
        "per_query": per_query,
    }


METRIC_COLUMNS = ["recall@5", "recall@10", "mrr@10", "ndcg@10"]


def gate_checks(result: dict, thresholds: dict[str, float]) -> list[dict]:
    return [{
        "metric": metric,
        "measured": round(result[metric], 4),
        "threshold": minimum,
        "pass": result[metric] >= minimum,
    } for metric, minimum in thresholds.items()]


def print_report(results: dict[str, dict], queries: int) -> None:
    print(f"queries={queries}  ranking depth={RETRIEVE_K} (doc-level)\n")
    header = f"{'mode':<8}" + "".join(f"{c:>11}" for c in METRIC_COLUMNS)
    print(header)
    print("-" * len(header))
    for mode, r in results.items():
        print(f"{mode:<8}" + "".join(f"{r[c]:>11.3f}" for c in METRIC_COLUMNS))
    print()
    for mode, r in results.items():
        misses = [q for q in r["per_query"] if q["first_hit_rank"] != 1]
        for q in misses:
            rank = q["first_hit_rank"] or "miss"
            print(f"  [{mode}] first hit @{rank}: {q['query']}")


def render_markdown(results: dict[str, dict], queries: int, gate: list[dict] | None,
                    gate_mode: str) -> str:
    lines = ["## Retrieval eval — bm25 vs vector vs hybrid", ""]
    lines.append(f"{queries} queries · doc-level ranking depth {RETRIEVE_K} · `{API}`")
    lines.append("")
    lines.append("| mode | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |")
    lines.append("|---|:---:|:---:|:---:|:---:|")
    for mode, r in results.items():
        cells = " | ".join(f"{r[c]:.3f}" for c in METRIC_COLUMNS)
        label = f"**{mode}**" if mode == gate_mode and gate is not None else mode
        lines.append(f"| {label} | {cells} |")
    lines.append("")
    if gate is not None:
        verdict = "✅ **PASS**" if all(c["pass"] for c in gate) else "❌ **FAIL**"
        lines.append(f"### Regression gate ({gate_mode}): {verdict}")
        lines.append("")
        lines.append("| metric | measured | threshold | |")
        lines.append("|---|:---:|:---:|:---:|")
        for c in gate:
            mark = "✅" if c["pass"] else "❌"
            lines.append(f"| {c['metric']} | {c['measured']:.3f} | ≥ {c['threshold']:.2f} | {mark} |")
        lines.append("")
    lines.append("<details><summary>First relevant rank per query</summary>")
    lines.append("")
    lines.append("| query | " + " | ".join(MODES) + " |")
    lines.append("|---|" + ":---:|" * len(MODES))
    for i, ex in enumerate(results[MODES[0]]["per_query"]):
        ranks = []
        for mode in MODES:
            rank = results[mode]["per_query"][i]["first_hit_rank"]
            ranks.append(str(rank) if rank else "—")
        query = ex["query"] if len(ex["query"]) <= 48 else ex["query"][:47] + "…"
        lines.append(f"| {query} | " + " | ".join(ranks) + " |")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="queries.example.jsonl",
                        help="JSONL with {query, relevant_doc_ids} per line")
    parser.add_argument("--json", metavar="PATH", help="write full results as JSON")
    parser.add_argument("--markdown", metavar="PATH",
                        help="write a markdown report (GitHub step summary friendly)")
    parser.add_argument("--gate", action="store_true",
                        help="enforce thresholds on --gate-mode; exit 1 on regression")
    parser.add_argument("--gate-mode", default="hybrid", choices=MODES)
    parser.add_argument("--min-recall5", type=float, default=0.90)
    parser.add_argument("--min-mrr10", type=float, default=0.85)
    parser.add_argument("--min-ndcg10", type=float, default=0.85)
    args = parser.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    results = {mode: evaluate_mode(examples, mode) for mode in MODES}
    print_report(results, len(examples))

    gate = None
    if args.gate:
        thresholds = {"recall@5": args.min_recall5, "mrr@10": args.min_mrr10,
                      "ndcg@10": args.min_ndcg10}
        gate = gate_checks(results[args.gate_mode], thresholds)
        for c in gate:
            status = "PASS" if c["pass"] else "FAIL"
            print(f"gate[{args.gate_mode}] {c['metric']}: {c['measured']:.3f} "
                  f">= {c['threshold']:.2f} ... {status}")

    if args.json:
        payload = {
            "api": API,
            "queries": len(examples),
            "retrieved_k": RETRIEVE_K,
            "modes": results,
            "gate": None if gate is None else {
                "mode": args.gate_mode,
                "checks": gate,
                "pass": all(c["pass"] for c in gate),
            },
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(results, len(examples), gate, args.gate_mode))

    if gate is not None and not all(c["pass"] for c in gate):
        print("\nregression gate FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
