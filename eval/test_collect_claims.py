"""Tests for collect_claims.py: observe mode -> a file a person can label -> a certificate.

Runs against a local SSE stub that speaks the backend's wire format (Spring writes
`data:` with no space), so no stack is needed.
"""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import collect_claims as cc
import factuality as fa

ANSWER = "BM25 ranks by term statistics [1]. It was invented in 2031. RRF fuses both."
SPANS = [(0, 34), (35, 59), (60, 75)]


def report(threshold=None, scores=(0.91, 0.07, 0.88)):
    return {"alpha": 0.1, "threshold": threshold, "certified": False,
            "claims": [{"start": s, "end": e, "score": sc, "kept": True}
                       for (s, e), sc in zip(SPANS, scores)]}


class Stub(BaseHTTPRequestHandler):
    """Behaviour is chosen by the question text."""

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)["q"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def ev(name, data):
            self.wfile.write(f"event:{name}\ndata:{data}\n\n".encode())

        if q == "cached":
            ev("cache", "hit")
        for i in range(0, len(ANSWER), 7):
            ev("token", json.dumps(ANSWER[i:i + 7]))
        if q == "calibrated":
            ev("claims", json.dumps(report(threshold=0.5)))
        elif q == "unscored":
            ev("claims", json.dumps(report(scores=(0.9, None, None))))
        elif q != "abstained":
            ev("claims", json.dumps(report()))
        ev("done", "")

    def log_message(self, *a):
        pass


class CollectClaims(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._old = cc.API
        cc.API = self.api

    def tearDown(self):
        cc.API = self._old

    def _tmp(self, rows=None):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows or []:
                f.write(json.dumps(r) + chr(10))
        self.addCleanup(os.remove, path)
        return path

    def test_sse_parsing_reassembles_the_streamed_answer_and_report(self):
        result = cc.ask("plain", api=self.api)
        self.assertEqual(result["answer"], ANSWER)
        self.assertEqual(len(result["claims"]["claims"]), 3)

    def test_rows_carry_the_claim_text_by_its_offsets_and_an_empty_label(self):
        row, why = cc.to_row("q", "g", cc.ask("plain", api=self.api))
        self.assertIsNone(why)
        self.assertEqual([c["text"] for c in row["claims"]], [
            "BM25 ranks by term statistics [1].", "It was invented in 2031.", "RRF fuses both."])
        self.assertTrue(all(c["supported"] is None for c in row["claims"]))

    def test_answers_the_filter_would_not_see_are_skipped_with_a_reason(self):
        out = self._tmp()
        questions = self._tmp([{"query": q} for q in
                               ("plain", "cached", "abstained", "calibrated", "unscored")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            cc.main([questions, out])
        printed = buf.getvalue()
        self.assertIn("1 answers, 3 claims to label", printed)
        for reason in ("cache hit", "no claims", "not in observe mode", "unscored claims"):
            self.assertIn(reason, printed)

    def test_an_unlabelled_file_is_refused_and_a_labelled_one_certifies(self):
        out = self._tmp()
        questions = self._tmp([{"query": "plain"}] * 30)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cc.main([questions, out])

        # Left as collected: null labels must not be read as "unsupported".
        with self.assertRaises(ValueError):
            fa.load_labels(out)

        with open(out, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        for r in rows:
            for c in r["claims"]:
                c["supported"] = "2031" not in c["text"]
        labelled = self._tmp(rows)
        answers, _ = fa.load_labels(labelled)
        cert = fa.calibrate_strict(answers, alpha=0.1)
        # The invented claim scored 0.07: the certified threshold drops it, keeps the others.
        self.assertTrue(cert.usable)
        self.assertGreaterEqual(cert.threshold, 0.07)
        self.assertLess(cert.threshold, 0.88)


if __name__ == "__main__":
    unittest.main()
