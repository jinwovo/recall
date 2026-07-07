"""RAG QA eval harness (docs/adr/0004).

Drives /api/ask (SSE) over a labeled query set and aggregates answer-quality signals:
groundedness verdicts from the post-hoc LLM judge, citation coverage, abstention rate,
TTFT and end-to-end latency. The groundedness % here is the README/eval number.

Usage:
    python run_qa_eval.py gold.jsonl
Env:
    RECALL_API (default http://localhost:8080), QA_TIMEOUT_S (default 300, per query)
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

API = os.getenv("RECALL_API", "http://localhost:8080")
TIMEOUT_S = int(os.getenv("QA_TIMEOUT_S", "300"))
CITATION = re.compile(r"\[\d+\]")
IDK = "I don't know based on the available documents."


def ask(query: str) -> dict:
    """Consume one SSE answer stream; return events of interest."""
    url = f"{API}/api/ask?q=" + urllib.parse.quote(query)
    out = {"answer": "", "verdict": None, "cache_hit": False, "ttft_ms": None, "total_ms": None}
    start = time.monotonic()
    event = ""
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].lstrip()
                if event == "token":
                    if out["ttft_ms"] is None:
                        out["ttft_ms"] = (time.monotonic() - start) * 1000
                    out["answer"] += json.loads(data)  # tokens are JSON-encoded strings
                elif event == "groundedness":
                    out["verdict"] = json.loads(data)["verdict"]
                elif event == "cache":
                    out["cache_hit"] = True
                elif event == "error":
                    raise RuntimeError(f"backend error event: {data}")
    out["total_ms"] = (time.monotonic() - start) * 1000
    return out


def pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole else "n/a"


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    results = []
    print(f"queries={len(examples)}  api={API}\n")
    print(f"{'#':<3}{'verdict':<14}{'cache':<7}{'ttft':>8}{'total':>9}  query")
    print("-" * 88)
    for i, ex in enumerate(examples, 1):
        r = ask(ex["query"])
        r["abstained"] = IDK in r["answer"]
        r["cited"] = bool(CITATION.search(r["answer"]))
        results.append(r)
        verdict = r["verdict"] or ("abstained" if r["abstained"] else "unjudged")
        ttft = f"{r['ttft_ms']:.0f}ms" if r["ttft_ms"] is not None else "-"
        print(f"{i:<3}{verdict:<14}{'hit' if r['cache_hit'] else '-':<7}"
              f"{ttft:>8}{r['total_ms']:>8.0f}ms  {ex['query'][:44]}")

    judged = [r for r in results if r["verdict"]]
    answered = [r for r in results if not r["abstained"] and not r["cache_hit"]]
    score = {"SUPPORTED": 1.0, "PARTIAL": 0.5, "UNSUPPORTED": 0.0}
    ttfts = [r["ttft_ms"] for r in results if r["ttft_ms"] is not None]

    print("\n-- summary " + "-" * 33)
    print(f"judged           {len(judged)}/{len(results)} "
          f"(abstained {sum(r['abstained'] for r in results)}, "
          f"cache hits {sum(r['cache_hit'] for r in results)})")
    for v in ("SUPPORTED", "PARTIAL", "UNSUPPORTED"):
        print(f"  {v.lower():<15}{pct(sum(r['verdict'] == v for r in judged), len(judged))}")
    if judged:
        print(f"groundedness     {statistics.mean(score[r['verdict']] for r in judged):.2f} (avg judge score)")
    print(f"citation coverage {pct(sum(r['cited'] for r in answered), len(answered))} of generated answers")
    if ttfts:
        print(f"TTFT p50         {statistics.median(ttfts):.0f}ms")
    print(f"e2e p50          {statistics.median(r['total_ms'] for r in results):.0f}ms")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "gold.jsonl")
