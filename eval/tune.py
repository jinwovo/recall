"""Self-tuning sweep for hybrid retrieval (docs/adr/0010).

Probes a grid of retrieval knobs — RRF k and fused candidate count — through the public
API's clamped tuning overrides (`/api/search?...&rrfK=&candidates=`), scores each combo
on the gold set with the same doc-level metrics as the CI gate, and compares against the
configured baseline parsed from application.yml. With --apply it rewrites the baseline
defaults in place; the nightly workflow turns an improvement into a pull request whose
eval gate then has to prove it (docs/adr/0007).

Usage:
    python tune.py gold.jsonl --report tune-report.md --json tune-result.json
    python tune.py gold.jsonl --apply          # rewrite application.yml with the winner
Env:
    RECALL_API (default http://localhost:8080)

Stdlib only. The winner must beat the baseline MRR@10 by --epsilon (default 0.01) —
run-to-run jitter must not generate churn PRs.
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

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
APPLICATION_YML = Path(__file__).resolve().parent.parent / "backend/src/main/resources/application.yml"
RETRIEVE_K = 10
GRID_RRF_K = (20, 60, 120)
GRID_CANDIDATES = (25, 50, 100)


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


def evaluate(examples: list[dict], rrf_k: int, candidates: int) -> dict:
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
    n = len(examples) or 1
    return {"rrf_k": rrf_k, "candidates": candidates,
            "recall5": sum(recalls) / n, "mrr10": sum(rrs) / n, "ndcg10": sum(ndcgs) / n}


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


def render_report(results: list[dict], base: dict, best: dict, improved: bool) -> str:
    lines = ["## Retrieval self-tuning sweep", "",
             f"grid over `rrf-k` × `candidates`, hybrid mode, {len(results)} combos · `{API}`", "",
             "| `rrf-k` | `candidates` | `Recall@5` | `MRR@10` | `nDCG@10` | |"]
    lines.append("|:---:|:---:|:---:|:---:|:---:|---|")
    for r in sorted(results, key=lambda x: (-x["mrr10"], -x["ndcg10"])):
        tag = []
        if r is best:
            tag.append("**winner**")
        if r["rrf_k"] == base["rrf_k"] and r["candidates"] == base["candidates"]:
            tag.append("baseline")
        lines.append(f"| {r['rrf_k']} | {r['candidates']} | {r['recall5']:.3f} "
                     f"| {r['mrr10']:.3f} | {r['ndcg10']:.3f} | {' '.join(tag)} |")
    lines.append("")
    if improved:
        lines.append(f"Winner beats the baseline `MRR@10` by "
                     f"**{best['mrr10'] - base['mrr10']:+.3f}** "
                     f"(`nDCG@10` {best['ndcg10'] - base['ndcg10']:+.3f}) — proposing "
                     f"`rrf-k: {best['rrf_k']}`, `candidates: {best['candidates']}`.")
    else:
        lines.append("No combo beats the baseline beyond epsilon — current defaults stand.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="gold.jsonl")
    parser.add_argument("--epsilon", type=float, default=0.01,
                        help="minimum MRR@10 lift over baseline to count as an improvement")
    parser.add_argument("--report", metavar="PATH", help="write a markdown report")
    parser.add_argument("--json", metavar="PATH", help="write machine-readable result")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite application.yml defaults with the winner (if improved)")
    args = parser.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    base_rrf_k, base_candidates = baseline_config()
    combos = sorted({(k, c) for k in GRID_RRF_K for c in GRID_CANDIDATES}
                    | {(base_rrf_k, base_candidates)})

    results = []
    for rrf_k, candidates in combos:
        r = evaluate(examples, rrf_k, candidates)
        results.append(r)
        print(f"rrf-k={rrf_k:<4} candidates={candidates:<4} "
              f"recall@5={r['recall5']:.3f} mrr@10={r['mrr10']:.3f} ndcg@10={r['ndcg10']:.3f}")

    base = next(r for r in results
                if r["rrf_k"] == base_rrf_k and r["candidates"] == base_candidates)
    best = max(results, key=lambda r: (r["mrr10"], r["ndcg10"], r["recall5"]))
    improved = best["mrr10"] >= base["mrr10"] + args.epsilon

    print(f"\nbaseline rrf-k={base_rrf_k} candidates={base_candidates} mrr@10={base['mrr10']:.3f}")
    print("IMPROVED" if improved else "NO_CHANGE",
          f"-> rrf-k={best['rrf_k']} candidates={best['candidates']} mrr@10={best['mrr10']:.3f}")

    if args.report:
        Path(args.report).write_text(render_report(results, base, best, improved), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"improved": improved, "baseline": base, "best": best, "epsilon": args.epsilon},
            indent=2), encoding="utf-8")
    if args.apply and improved:
        apply_config(best["rrf_k"], best["candidates"])
        print(f"applied to {APPLICATION_YML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
