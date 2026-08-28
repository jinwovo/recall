"""RAG QA eval harness (docs/adr/0004, 0015).

Drives /api/ask (SSE) over a labeled query set and aggregates answer-quality signals:
groundedness verdicts from the post-hoc LLM judge, citation coverage, abstention rate,
TTFT and end-to-end latency.

The groundedness number is the judge grading the system it belongs to, which is an opinion
rather than a measurement. With `--human-labels` it becomes a measurement: hand-grade a
subset, and prediction-powered inference (docs/adr/0015) measures the judge's bias on those
and subtracts it, producing an interval for the *true* groundedness that is valid however
biased the judge turns out to be.

Usage:
    python run_qa_eval.py gold.jsonl
    python run_qa_eval.py gold.jsonl --human-labels judge-labels.jsonl
Env:
    RECALL_API (default http://localhost:8080), QA_TIMEOUT_S (default 300, per query)
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

import ppi

# Keep console output alive on narrow encodings (e.g. Windows cp949) — degrade, don't crash.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

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


def load_human_labels(path: str) -> dict[str, float]:
    """query -> hand-assigned groundedness on the judge's own 0 / 0.5 / 1 scale.

    Same scale as the judge, because prediction-powered inference measures the gap between
    the two and a rescaling would be indistinguishable from bias.
    """
    labels = {}
    for number, line in enumerate(open(path, encoding="utf-8"), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        value = row.get("groundedness")
        if value is None:
            raise SystemExit(f"{path}:{number}: needs a 'groundedness' score")
        if not 0.0 <= float(value) <= 1.0:
            raise SystemExit(f"{path}:{number}: groundedness must be in [0, 1]")
        labels[row["query"]] = float(value)
    return labels


def report_ppi(judged: list[dict], scores: dict[str, float], labels: dict[str, float]) -> None:
    """The judge's number, and what it is actually worth against the hand labels."""
    paired = [(labels[r["query"]], scores[r["verdict"]])
              for r in judged if r["query"] in labels]
    unpaired = [scores[r["verdict"]] for r in judged if r["query"] not in labels]

    print()
    print("-- groundedness, corrected " + "-" * 18)
    if len(paired) < 2 or not unpaired:
        # Refuse rather than produce something that looks like a guarantee. Two labelled
        # answers cannot estimate a bias, and with nothing left unlabelled there is no
        # judge contribution to correct in the first place.
        print(f"  {len(paired)} hand-labelled and {len(unpaired)} judge-only answers — "
              f"needs at least 2 and 1.")
        print("  Hand-label more answers, or drop --human-labels and quote the judge as an "
              "opinion.")
        return

    estimate = ppi.ppi_mean([y for y, _ in paired], [f for _, f in paired], unpaired)
    print(f"  judge alone      {estimate.judge_only:.3f}   (no validity — this is the "
          f"judge's opinion of its own system)")
    lo, hi = estimate.labeled_only_ci
    print(f"  hand labels only {estimate.labeled_only:.3f}   [{lo:.3f}, {hi:.3f}]  "
          f"n={estimate.n_labeled}")
    print(f"  PPI              {estimate.estimate:.3f}   [{estimate.lo:.3f}, "
          f"{estimate.hi:.3f}]  lambda={estimate.lam:.2f}")
    print(f"  judge bias       {estimate.judge_bias:+.3f}   "
          f"({'optimistic' if estimate.judge_is_optimistic else 'pessimistic'})")
    print(f"  effective labels {estimate.effective_n:.0f}   (from {estimate.n_labeled} "
          f"actually written)")


def main(path: str, human_labels: str | None = None) -> None:
    with open(path, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    results = []
    print(f"queries={len(examples)}  api={API}\n")
    print(f"{'#':<3}{'verdict':<14}{'cache':<7}{'ttft':>8}{'total':>9}  query")
    print("-" * 88)
    for i, ex in enumerate(examples, 1):
        r = ask(ex["query"])
        r["query"] = ex["query"]
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

    if human_labels and judged:
        report_ppi(judged, score, load_human_labels(human_labels))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="gold.jsonl")
    parser.add_argument("--human-labels", metavar="PATH",
                        help="JSONL of {query, groundedness} hand grades, for docs/adr/0015")
    args = parser.parse_args()
    main(args.gold, args.human_labels)
