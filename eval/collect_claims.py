"""Collect claims from serving's observe mode into a file a person can label (docs/adr/0017).

    RECALL_API=http://localhost:8080 python collect_claims.py gold.jsonl claims-to-label.jsonl

Runs each question through /api/ask with the claim filter in observe mode
(recall.rag.factuality.enabled=true, threshold=-1), and writes one row per answer:

    {"query": "...", "group": "...",
     "claims": [{"text": "...", "score": 0.91, "supported": null}, ...]}

A person replaces every ``null`` with true or false — "do the passages this answer was
generated from support this sentence?" — and the file goes straight into
``python factuality.py claims-to-label.jsonl``, which refuses any claim left unlabelled.

Answers served from the semantic cache, abstentions and answers whose claims were not all
scored are skipped and counted: the threshold has to be calibrated on the population it
will filter, and none of those is filtered.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

API = os.getenv("RECALL_API", "http://localhost:8080")
ASK_TIMEOUT = float(os.getenv("RECALL_ASK_TIMEOUT", "300"))


def read_sse(lines):
    """Yield (event, data) pairs from an iterable of decoded SSE lines."""
    event, data = "message", []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)   # one space, per spec
    if data:
        yield event, "\n".join(data)


def ask(query: str, api: str | None = None) -> dict:
    """The answer text and the claims report for one question (None when there is none)."""
    url = f"{api or API}/api/ask?q=" + urllib.parse.quote(query)
    answer, report, cached, error = [], None, False, None
    with urllib.request.urlopen(url, timeout=ASK_TIMEOUT) as r:
        lines = (b.decode("utf-8") for b in r)
        for event, data in read_sse(lines):
            if event == "token":
                answer.append(json.loads(data))
            elif event == "claims":
                report = json.loads(data)
            elif event == "cache":
                cached = True
            elif event == "error":
                error = data
            elif event == "done":
                break
    return {"answer": "".join(answer), "claims": report, "cached": cached, "error": error}


def to_row(query: str, group, result: dict) -> tuple[dict | None, str | None]:
    """A labelling row, or the reason this answer cannot be one."""
    if result["error"]:
        return None, "error"
    if result["cached"]:
        return None, "cache hit"
    report = result["claims"]
    if not report or not report.get("claims"):
        return None, "no claims (abstained, or the filter is off)"
    if report.get("threshold") is not None:
        # Collecting under an active filter would label only the claims it let through.
        return None, "filter is calibrated, not in observe mode"
    claims = report["claims"]
    if any(c.get("score") is None for c in claims):
        return None, "unscored claims (raise recall.rag.factuality.max-claims)"
    text = result["answer"]
    return {"query": query, "group": group,
            "claims": [{"text": text[c["start"]:c["end"]], "score": c["score"],
                        "supported": None} for c in claims]}, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("questions", help="JSONL with a 'query' field (gold.jsonl works)")
    ap.add_argument("out", help="where to write the rows to label")
    ap.add_argument("--group-field", help="copy this field of each question into 'group'")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    with open(args.questions, encoding="utf-8") as f:
        questions = [json.loads(line) for line in f if line.strip()][:args.limit]

    skipped: dict[str, int] = {}
    written = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for i, q in enumerate(questions, start=1):
            try:
                result = ask(q["query"])
            except OSError as e:
                result = {"error": str(e), "cached": False, "claims": None, "answer": ""}
            row, why = to_row(q["query"], q.get(args.group_field) if args.group_field else None,
                              result)
            if row is None:
                skipped[why] = skipped.get(why, 0) + 1
            else:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
            print(f"\r{i}/{len(questions)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    claims = 0
    with open(args.out, encoding="utf-8") as f:
        for line in f:
            claims += len(json.loads(line)["claims"])
    print(f"{written} answers, {claims} claims to label -> {args.out}")
    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n}: {why}")
    if written < 9:
        print("  fewer than 9 answers cannot certify alpha = 0.10 at all")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
