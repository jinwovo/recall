"""Retrieval eval harness (docs/adr/0001).

Sweeps BM25-only / vector-only / hybrid against a labeled query set and prints a
comparison table (Recall@K, MRR@K, nDCG@K) — the hybrid lift is the README headline.

Usage:
    python run_eval.py queries.example.jsonl
Env:
    RECALL_API (default http://localhost:8080), EVAL_K (default 10)
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request

API = os.getenv("RECALL_API", "http://localhost:8080")
K = int(os.getenv("EVAL_K", "10"))
MODES = ["bm25", "vector", "hybrid"]


def search(query: str, mode: str) -> list[str]:
    url = f"{API}/api/search?q=" + urllib.parse.quote(query) + f"&mode={mode}"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    return [c["docId"] for c in data.get("results", [])]


def metrics(examples: list[dict], mode: str) -> tuple[float, float, float]:
    recalls, rrs, ndcgs = [], [], []
    for ex in examples:
        gold = set(ex["relevant_doc_ids"])
        ranked = search(ex["query"], mode)[:K]

        hits = sum(1 for d in ranked if d in gold)
        recalls.append(hits / len(gold) if gold else 0.0)
        rrs.append(next((1.0 / (i + 1) for i, d in enumerate(ranked) if d in gold), 0.0))

        dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked) if d in gold)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), K)))
        ndcgs.append(dcg / idcg if idcg else 0.0)

    n = len(examples) or 1
    return sum(recalls) / n, sum(rrs) / n, sum(ndcgs) / n


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    print(f"queries={len(examples)}  K={K}\n")
    print(f"{'mode':<8}{'Recall@K':>12}{'MRR@K':>10}{'nDCG@K':>10}")
    print("-" * 40)
    for mode in MODES:
        r, m, n = metrics(examples, mode)
        print(f"{mode:<8}{r:>12.3f}{m:>10.3f}{n:>10.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "queries.example.jsonl")
