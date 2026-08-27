"""Tests for the BEIR converter (eval/beir.py).

Everything network-facing is stubbed, so these run offline. What is actually being checked
is the conversion: a judgement pointing at a document that is not in the corpus, a query
with no usable judgements left, or a subset taken off the head of an id-ordered file would
each quietly change what the benchmark measures, and none of them would fail loudly.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import json
import tempfile
import unittest
from pathlib import Path

import beir

CORPUS = [
    {"_id": "d1", "title": "Titled", "text": "body one"},
    {"_id": "d2", "title": "", "text": "body two"},
    {"_id": "d3", "title": "Only a title", "text": ""},
    {"_id": "d4", "title": "", "text": ""},          # empty: nothing to index
]
QUERIES = [{"_id": f"q{i}", "text": f"question {i}"} for i in range(1, 6)]
QRELS = {
    "q1": ["d1"],
    "q2": ["d2", "missing-from-corpus"],
    "q3": ["also-missing"],                          # every judgement dangles
    "q4": ["d3"],
    # q5 has no judgements at all
}


class BuildTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._rows, self._qrels = beir.fetch_rows, beir.fetch_qrels
        beir.fetch_rows = lambda dataset, config, label, checkpoint=None: (
            CORPUS if config == "corpus" else QUERIES)
        beir.fetch_qrels = lambda dataset, split, minimum: {k: set(v) for k, v in QRELS.items()}
        self.addCleanup(lambda: (setattr(beir, "fetch_rows", self._rows),
                                 setattr(beir, "fetch_qrels", self._qrels)))

    def build(self, **kwargs):
        options = {"dataset": "scifact", "split": "test", "min_relevance": 1,
                   "max_queries": None, "cache": Path(self.tmp.name) / "cache"}
        options.update(kwargs)
        return beir.build(**options)

    def test_title_and_body_are_joined_and_empty_documents_dropped(self):
        corpus, _, dropped = self.build()
        by_id = {record["docId"]: record for record in corpus}
        self.assertEqual(by_id["d1"]["content"], "Titled\n\nbody one")
        self.assertEqual(by_id["d2"]["content"], "body two")       # no title, no leading gap
        self.assertEqual(by_id["d3"]["content"], "Only a title")
        self.assertNotIn("d4", by_id)
        self.assertEqual(dropped["empty_documents"], 1)

    def test_records_carry_the_fields_the_ingest_pipeline_expects(self):
        corpus, gold, _ = self.build()
        self.assertEqual(set(corpus[0]), {"docId", "source", "lang", "content"})
        self.assertEqual(corpus[0]["source"], "beir/scifact")
        self.assertEqual(set(gold[0]), {"query", "relevant_doc_ids"})

    def test_judgements_outside_the_corpus_are_dropped_and_counted(self):
        _, gold, dropped = self.build()
        by_query = {g["query"]: g["relevant_doc_ids"] for g in gold}
        # q2 keeps its one real judgement; the dangling one goes and is reported.
        self.assertEqual(by_query["question 2"], ["d2"])
        self.assertEqual(dropped["judgements_pointing_outside_the_corpus"], 2)

    def test_a_query_left_with_no_judgements_is_dropped(self):
        # q3's only judgement dangles, so it would score zero for a reason that has nothing
        # to do with retrieval; q5 was never judged at all.
        _, gold, dropped = self.build()
        queries = {g["query"] for g in gold}
        self.assertNotIn("question 3", queries)
        self.assertNotIn("question 5", queries)
        self.assertEqual(dropped["queries_without_judgements"], 1)

    def test_gold_order_is_stable(self):
        first = [g["query"] for g in self.build()[1]]
        second = [g["query"] for g in self.build()[1]]
        self.assertEqual(first, second)

    def test_a_subset_is_spread_across_the_split_not_taken_off_the_head(self):
        big_qrels = {f"q{i}": ["d1"] for i in range(1, 101)}
        big_queries = [{"_id": f"q{i}", "text": f"question {i}"} for i in range(1, 101)]
        beir.fetch_rows = lambda dataset, config, label, checkpoint=None: (
            CORPUS if config == "corpus" else big_queries)
        beir.fetch_qrels = lambda dataset, split, minimum: {k: set(v)
                                                            for k, v in big_qrels.items()}
        _, gold, _ = self.build(max_queries=10)
        self.assertEqual(len(gold), 10)
        full = [g["query"] for g in self.build()[1]]
        picked = [full.index(g["query"]) for g in gold]
        self.assertEqual(picked, sorted(picked))
        # Head-slicing would put every pick in the first tenth; even spacing does not.
        self.assertGreater(max(picked), len(full) * 0.8)

    def test_caching_avoids_a_second_fetch(self):
        cache = Path(self.tmp.name) / "cache"
        self.build(cache=cache)
        calls = []
        beir.fetch_rows = lambda *a, **k: calls.append(a) or []
        self.build(cache=cache)
        self.assertEqual(calls, [])


class QrelsParsingTest(unittest.TestCase):

    TSV = "query-id\tcorpus-id\tscore\nq1\td1\t2\nq1\td2\t1\nq2\td3\t0\nbroken-line\n"

    def setUp(self):
        self._text = beir.fetch_text
        beir.fetch_text = lambda url, attempts=4: self.TSV
        self.addCleanup(lambda: setattr(beir, "fetch_text", self._text))

    def test_header_and_malformed_lines_are_skipped(self):
        parsed = beir.fetch_qrels("nfcorpus", "test", 1)
        self.assertEqual(parsed, {"q1": {"d1", "d2"}})

    def test_the_relevance_cutoff_is_applied(self):
        self.assertEqual(beir.fetch_qrels("nfcorpus", "test", 2), {"q1": {"d1"}})
        self.assertEqual(beir.fetch_qrels("nfcorpus", "test", 0),
                         {"q1": {"d1", "d2"}, "q2": {"d3"}})


class CatalogTest(unittest.TestCase):

    def test_scifact_is_the_default_and_has_binary_judgements(self):
        # It is the default precisely because binary qrels make nDCG comparable to the
        # published numbers; a graded dataset binarised here would not be.
        self.assertIn("scifact", beir.CATALOG)
        self.assertFalse(beir.CATALOG["scifact"].graded)

    def test_graded_datasets_are_marked_so_the_warning_can_fire(self):
        self.assertTrue(beir.CATALOG["nfcorpus"].graded)
        self.assertTrue(beir.CATALOG["trec-covid"].graded)

    def test_every_entry_carries_a_note(self):
        for benchmark in beir.CATALOG.values():
            self.assertTrue(benchmark.note)
            self.assertGreater(benchmark.docs, 0)
            self.assertGreater(benchmark.queries, 0)


class WriteTest(unittest.TestCase):

    def test_jsonl_round_trips_unicode_without_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "out.jsonl"
            beir.write_jsonl(path, [{"query": "한국어 질의"}, {"query": "plain"}])
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("한국어", lines[0])
            self.assertEqual(json.loads(lines[0])["query"], "한국어 질의")


if __name__ == "__main__":
    unittest.main(verbosity=2)
