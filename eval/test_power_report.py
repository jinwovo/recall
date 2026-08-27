"""Tests for the gold-set design analyser (eval/power_report.py).

The tool's whole value is that its answers are knowable before any retrieval happens, so
its answers are checkable against closed forms here — no stack, no fixtures, no network.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import json
import os
import tempfile
import unittest

import power_report as pr

SINGLE_LABEL = [{"query": f"q{i}", "relevant_doc_ids": [f"d{i}"]} for i in range(10)]
MULTI_LABEL = [{"query": f"q{i}", "relevant_doc_ids": [f"d{i}a", f"d{i}b"]} for i in range(4)]


class GoldShapeTest(unittest.TestCase):

    def test_one_label_per_query_makes_recall_binomial(self):
        shape = pr.gold_shape(SINGLE_LABEL)
        self.assertEqual(shape["n"], 10)
        self.assertEqual((shape["min_relevant"], shape["max_relevant"]), (1, 1))
        self.assertTrue(shape["recall_is_binomial"])

    def test_multiple_labels_make_recall_graded(self):
        shape = pr.gold_shape(MULTI_LABEL)
        self.assertEqual(shape["n"], 4)
        self.assertEqual(shape["mean_relevant"], 2.0)
        self.assertFalse(shape["recall_is_binomial"])

    def test_mixed_label_counts_report_the_range(self):
        shape = pr.gold_shape(SINGLE_LABEL[:2] + MULTI_LABEL[:2])
        self.assertEqual((shape["min_relevant"], shape["max_relevant"]), (1, 2))
        self.assertFalse(shape["recall_is_binomial"])

    def test_empty_gold_set_does_not_crash(self):
        self.assertEqual(pr.gold_shape([])["n"], 0)


class LadderTest(unittest.TestCase):

    def test_p_floor_ladder_is_the_analytic_two_over_two_to_the_k(self):
        ladder = pr.p_floor_ladder(10)
        self.assertEqual(len(ladder), 10)
        for row in ladder:
            self.assertAlmostEqual(row["floor_p"], min(1.0, 2.0 ** (1 - row["differing"])),
                                   places=12)

    def test_six_differing_queries_is_the_threshold_at_n_ten(self):
        # 2^-4 = 0.0625 is above 0.05; 2^-5 = 0.03125 is below. Six is the first k that
        # permits a significant result, which is the headline this tool exists to print.
        by_k = {row["differing"]: row for row in pr.p_floor_ladder(10)}
        self.assertFalse(by_k[5]["can_reach_significance"])
        self.assertTrue(by_k[6]["can_reach_significance"])

    def test_interval_ladder_has_no_duplicate_rows_at_small_n(self):
        # 0.95 and 1.00 both round onto 10/10 when n = 10 — one row, not two.
        rows = pr.interval_ladder(10)
        self.assertEqual(len({r["successes"] for r in rows}), len(rows))

    def test_interval_ladder_matches_the_closed_form_at_a_perfect_score(self):
        perfect = next(r for r in pr.interval_ladder(10) if r["successes"] == 10)
        self.assertAlmostEqual(perfect["lo"], 0.025 ** 0.1, places=9)
        self.assertEqual(perfect["hi"], 1.0)

    def test_intervals_narrow_as_the_gold_set_grows(self):
        small = next(r for r in pr.interval_ladder(10) if r["observed"] == 1.0)
        large = next(r for r in pr.interval_ladder(300) if r["observed"] == 1.0)
        self.assertLess(large["width"], small["width"])

    def test_sample_size_grows_as_the_effect_shrinks(self):
        rows = pr.sample_size_table([0.30], [0.20, 0.10, 0.05, 0.02, 0.01])
        counts = [r["queries"] for r in rows]
        self.assertEqual(counts, sorted(counts))
        # Resolving a 0.01 MRR move at sd = 0.30 needs thousands of queries, not ten.
        self.assertGreater(counts[-1], 1000)


class ObservedSpreadTest(unittest.TestCase):

    def write_results(self, payload: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def payload(self) -> dict:
        def mode(rrs):
            return {"per_query": [{"rr": rr} for rr in rrs]}
        return {
            "modes": {
                "bm25": mode([1.0, 0.5, 0.0, 1.0, 0.2, 1.0]),
                "hybrid": mode([1.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
            },
            "comparison": {"baseline": "bm25"},
        }

    def test_reads_the_measured_spread_and_skips_the_baseline(self):
        observed = pr.observed_spreads(self.write_results(self.payload()))
        self.assertEqual(observed["baseline"], "bm25")
        self.assertNotIn("bm25", observed["modes"])
        hybrid = observed["modes"]["hybrid"]
        self.assertGreater(hybrid["sd"], 0.0)
        self.assertGreater(hybrid["mde"], 0.0)
        # Three of six queries tie, so the floor p is 2^-2 = 0.25.
        self.assertAlmostEqual(hybrid["floor_p"], 0.25, places=12)

    def test_falls_back_to_the_first_mode_when_no_comparison_was_recorded(self):
        payload = self.payload()
        del payload["comparison"]
        self.assertEqual(pr.observed_spreads(self.write_results(payload))["baseline"], "bm25")

    def test_rejects_a_file_that_is_not_an_eval_result(self):
        with self.assertRaises(SystemExit):
            pr.observed_spreads(self.write_results({"something": "else"}))


class RenderTest(unittest.TestCase):

    def test_console_and_markdown_both_carry_the_three_findings(self):
        shape = pr.gold_shape(SINGLE_LABEL)
        for markdown in (False, True):
            text = pr.render(shape, None, markdown=markdown)
            self.assertIn("p-value floor", text)
            self.assertIn("interval you are stuck with", text)
            self.assertIn("queries you would need", text)
            # The count is emphasised in markdown and bare on the console; the claim is one
            # sentence either way.
            self.assertIn("of 10 queries must differ before any outcome can be significant",
                          text)
        self.assertIn("At least 6 of 10", pr.render(shape, None, markdown=False))

    def test_markdown_emits_tables(self):
        text = pr.render(pr.gold_shape(SINGLE_LABEL), None, markdown=True)
        self.assertIn("| differing queries | floor p |", text)
        self.assertIn("|:---:|", text)

    def test_states_when_a_gold_set_cannot_support_significance_at_all(self):
        text = pr.render(pr.gold_shape(SINGLE_LABEL[:4]), None, markdown=False)
        self.assertIn("cannot support a significance claim", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
