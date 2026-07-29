"""Seed the eval corpus and wait until async indexing converges (docs/adr/0007).

Ingestion is async (Kafka → chunk → embed → upsert, docs/adr/0003), so "seeded" and
"searchable" are different moments. CI cannot sleep-and-hope: this script POSTs every
corpus document, then polls Elasticsearch for the distinct set of indexed docIds until
all expected documents are searchable — a deterministic barrier for the eval gate.

Usage:
    python scripts/seed_corpus.py [eval/corpus.jsonl] [--timeout 300]
Env:
    RECALL_API      backend base URL   (default http://localhost:8080)
    RECALL_ES       Elasticsearch URL  (default http://localhost:9200)
    RECALL_ES_INDEX index name         (default recall-docs)

Stdlib only — no dependencies to install in CI. Re-running is safe: chunk upserts are
idempotent by content hash.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
ES = os.getenv("RECALL_ES", "http://localhost:9200")
INDEX = os.getenv("RECALL_ES_INDEX", "recall-docs")


def post_json(url: str, payload: dict, attempts: int = 3) -> int:
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as r:
                return r.status
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"POST {url} failed after {attempts} attempts") from last


def indexed_doc_ids() -> set[str]:
    query = {"size": 0, "aggs": {"docs": {"terms": {"field": "docId", "size": 10_000}}}}
    request = urllib.request.Request(
        f"{ES}/{INDEX}/_search", data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:          # index not created yet — nothing indexed
            return set()
        raise
    except (urllib.error.URLError, TimeoutError):
        return set()               # ES still starting — the poll loop retries
    buckets = data.get("aggregations", {}).get("docs", {}).get("buckets", [])
    return {b["key"] for b in buckets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("corpus", nargs="?", default="eval/corpus.jsonl")
    parser.add_argument("--timeout", type=int, default=300,
                        help="seconds to wait for indexing to converge")
    args = parser.parse_args()

    with open(args.corpus, encoding="utf-8") as f:
        documents = [json.loads(line) for line in f if line.strip()]
    expected = {d["docId"] for d in documents}

    print(f"seeding {len(documents)} documents -> {API}/api/ingest")
    for doc in documents:
        status = post_json(f"{API}/api/ingest", doc)
        if status != 202:
            print(f"unexpected status {status} for {doc['docId']}", file=sys.stderr)
            return 1

    print(f"waiting for async indexing (barrier: {len(expected)} docIds in {INDEX})")
    deadline = time.monotonic() + args.timeout
    last_report = 0.0
    while time.monotonic() < deadline:
        indexed = indexed_doc_ids() & expected
        if indexed == expected:
            print(f"indexed {len(expected)}/{len(expected)} documents — corpus ready")
            return 0
        if time.monotonic() - last_report >= 5:
            missing = sorted(expected - indexed)
            head = ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")
            print(f"  indexed {len(indexed)}/{len(expected)} (missing: {head})")
            last_report = time.monotonic()
        time.sleep(1)

    print(f"timed out after {args.timeout}s; missing: {sorted(expected - indexed_doc_ids())}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
