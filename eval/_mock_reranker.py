"""A stand-in search API with a realistic reranker, for testing the calibrator offline.

`calibrate.py` and the sequential gate both talk to `/api/search`, and both need to be
exercisable without booting Elasticsearch, a bge-m3 sidecar and a Kafka pipeline. This
serves the same JSON shape from synthetic queries whose difficulty is drawn from a mixture:
most have one obviously-right passage and a peaked score profile, a minority are ambiguous
with the answer buried and the scores nearly flat, and a configurable slice have no relevant
document anywhere — the upstream recall miss that no context size can repair.

Scores go through a logistic squash, which is what a cross-encoder head actually emits, and
is also the case where a badly chosen softmax temperature quietly destroys the benefit of
adaptive sizing.

Used by `test_calibrate.py`; runnable directly for a manual look:

    python _mock_reranker.py --queries 300
"""
from __future__ import annotations

import http.server
import json
import math
import random
import socketserver
import threading
import urllib.parse


def make_query(rng: random.Random, candidates: int = 20) -> tuple[list[float], int | None]:
    """Raw reranker logits in ranked order, and the index of the relevant passage."""
    ambiguous = rng.random() < 0.35
    if ambiguous:
        relevant = rng.randint(1, 10)
        peakedness = rng.uniform(0.05, 0.4)
    else:
        relevant = 0 if rng.random() < 0.85 else 1
        peakedness = rng.uniform(1.5, 4.0)
    logits = sorted((rng.gauss(0.0, 1.0) for _ in range(candidates)), reverse=True)
    return [x * peakedness for x in logits], relevant


class MockSearchServer:
    """A threaded `/api/search` backed by a fixed, seeded fixture."""

    def __init__(self, queries: int = 300, miss_rate: float = 0.08, seed: int = 31337,
                 candidates: int = 20):
        rng = random.Random(seed)
        self.fixture: dict[int, tuple[list[float], int | None]] = {}
        for i in range(queries):
            logits, relevant = make_query(rng, candidates)
            self.fixture[i] = (logits, None if rng.random() < miss_rate else relevant)
        self.queries = queries
        self.calls = 0
        fixture, owner = self.fixture, self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):        # keep the test output readable
                pass

            def do_GET(self):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                index = int(params["q"][0][1:])
                logits, relevant = fixture[index]
                owner.calls += 1
                results = [{
                    "docId": f"d{index}" if j == relevant else f"other-{index}-{j}",
                    "chunkId": f"c{index}-{j}",
                    # Logistic squash: a cross-encoder relevance head emits [0, 1], not
                    # raw logits, and that compressed scale is what makes the softmax
                    # temperature matter.
                    "score": 1.0 / (1.0 + math.exp(-logit)),
                    "content": f"passage {j} for query {index}",
                    "source": "mock",
                } for j, logit in enumerate(logits)]
                body = json.dumps({"results": results}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockSearchServer":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def gold_lines(self) -> list[str]:
        """The matching gold set, as JSONL text."""
        return [json.dumps({"query": f"q{i}", "relevant_doc_ids": [f"d{i}"]})
                for i in range(self.queries)]


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", type=int, default=300)
    parser.add_argument("--miss-rate", type=float, default=0.08)
    args = parser.parse_args()
    with MockSearchServer(args.queries, args.miss_rate) as server:
        print(f"mock search API on {server.url} ({args.queries} queries) — ctrl-c to stop")
        print(f"gold set: {server.gold_lines()[0]} ...")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
