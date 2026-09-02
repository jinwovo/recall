"""Tests for the eval harness itself (eval/run_eval.py).

An eval harness that gates CI is production code: if it computes nDCG wrong, or picks the
wrong interval, or lets a regression through, every number downstream of it is wrong and
nothing else in the build will say so. These tests run the whole harness offline against a
stubbed search backend whose rankings are fixed, so every metric has a value derivable by
hand and all four gate policies — including the sequential one, whose saving has to be
real API calls not made — can be driven into both outcomes.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import run_eval
import stats

# Ten queries, one relevant document each — the shape of eval/gold.jsonl.
GOLD = [{"query": f"q{i}", "relevant_doc_ids": [f"d{i}"]} for i in range(10)]

# Fixed rankings per mode. bm25 finds the gold document at rank 1 six times, rank 2 once,
# rank 5 once, and misses twice; hybrid finds it at rank 1 nine times and rank 2 once.
# Recall@5  bm25 = 8/10, hybrid = 10/10
# MRR@10    bm25 = (6*1 + 1/2 + 1/5) / 10 = 0.67,  hybrid = (9*1 + 1/2) / 10 = 0.95
FIRST_HIT_RANK = {
    "bm25":   [1, 1, 1, 1, 1, 1, 2, 5, None, None],
    "hybrid": [1, 1, 1, 1, 1, 1, 1, 1, 1, 2],
}


def stub_search(query: str, mode: str, attempts: int = 3) -> list[str]:
    """Deterministic ranking that plants the gold doc at a chosen rank (or not at all)."""
    index = int(query[1:])
    rank = FIRST_HIT_RANK[mode][index]
    ranked = [f"filler-{index}-{j}" for j in range(run_eval.RETRIEVE_K)]
    if rank is not None:
        ranked[rank - 1] = f"d{index}"
    return ranked


class HarnessTestCase(unittest.TestCase):
    """Swaps the HTTP call for the stub and evaluates both modes once per test."""

    def setUp(self):
        self._real_search = run_eval.search
        run_eval.search = stub_search
        self.addCleanup(lambda: setattr(run_eval, "search", self._real_search))
        self.results = {m: run_eval.evaluate_mode(GOLD, m) for m in ("bm25", "hybrid")}

    def with_intervals(self, iters: int = 2000):
        run_eval.add_intervals(self.results, iters)
        return self.results


class MetricTest(HarnessTestCase):

    def test_point_estimates_match_hand_calculation(self):
        bm25, hybrid = self.results["bm25"], self.results["hybrid"]
        self.assertAlmostEqual(bm25["recall@5"], 0.8, places=12)
        self.assertAlmostEqual(bm25["recall@10"], 0.8, places=12)
        self.assertAlmostEqual(bm25["mrr@10"], (6 + 0.5 + 0.2) / 10, places=12)
        self.assertAlmostEqual(hybrid["recall@5"], 1.0, places=12)
        self.assertAlmostEqual(hybrid["mrr@10"], (9 + 0.5) / 10, places=12)

    def test_ndcg_matches_the_single_relevant_document_closed_form(self):
        # One relevant document => ideal DCG is 1, so nDCG collapses to 1/log2(rank+1).
        expected = sum(0.0 if r is None else 1.0 / math.log2(r + 1)
                       for r in FIRST_HIT_RANK["bm25"]) / 10
        self.assertAlmostEqual(self.results["bm25"]["ndcg@10"], expected, places=12)

    def test_ndcg_never_exceeds_one(self):
        # The doc-level dedup exists so one document's chunks cannot occupy several ranks
        # and push DCG past the ideal; assert the invariant it protects.
        for result in self.results.values():
            for q in result["per_query"]:
                self.assertLessEqual(q["ndcg"], 1.0 + 1e-12)

    def test_per_query_records_carry_every_reported_metric(self):
        for metric, key in run_eval.METRIC_TO_QUERY_KEY.items():
            values = run_eval.values_of(self.results["bm25"], metric)
            self.assertEqual(len(values), len(GOLD))
            self.assertAlmostEqual(stats.mean(values), self.results["bm25"][metric], places=12)
            self.assertIn(key, self.results["bm25"]["per_query"][0])


class IntervalSelectionTest(HarnessTestCase):

    def test_binary_metric_uses_the_exact_binomial_interval(self):
        results = self.with_intervals()
        ci = results["hybrid"]["ci"]["recall@5"]
        self.assertEqual(ci["method"], "exact-binomial")
        self.assertEqual(ci["successes"], 10)
        # 10/10 is [0.69, 1.00] — the interval that keeps a perfect score honest.
        self.assertAlmostEqual(ci["lo"], 0.025 ** 0.1, places=9)
        self.assertEqual(ci["hi"], 1.0)

    def test_graded_metric_uses_the_bootstrap(self):
        results = self.with_intervals()
        ci = results["bm25"]["ci"]["mrr@10"]
        self.assertEqual(ci["method"], "bca-bootstrap")
        self.assertLess(ci["lo"], results["bm25"]["mrr@10"])
        self.assertGreater(ci["hi"], results["bm25"]["mrr@10"])

    def test_every_interval_brackets_its_point_estimate(self):
        results = self.with_intervals()
        for result in results.values():
            for metric in run_eval.METRIC_COLUMNS:
                ci = result["ci"][metric]
                self.assertLessEqual(ci["lo"], result[metric] + 1e-9)
                self.assertGreaterEqual(ci["hi"], result[metric] - 1e-9)


class ComparisonTest(HarnessTestCase):

    def test_recall_lift_on_two_differing_queries_is_unresolvable(self):
        # hybrid beats bm25 on Recall@5 by +0.20, but only the two bm25 misses differ, so
        # the floor p is 0.5 and no outcome of this test could have been significant.
        self.with_intervals()
        comparison = run_eval.compare_modes(self.results, "bm25", 2000)
        row = comparison["metrics"]["recall@5"][0]
        self.assertEqual(row["mode"], "hybrid")
        self.assertAlmostEqual(row["observed_diff"], 0.2, places=12)
        self.assertEqual(row["effective_n"], 2)
        self.assertAlmostEqual(row["min_attainable_p"], 0.5, places=12)
        self.assertTrue(row["underpowered"])
        self.assertFalse(row["significant"])
        self.assertIn("unresolvable", run_eval.verdict_of(row))

    def test_holm_adjustment_is_recorded_and_never_below_the_raw_p(self):
        self.with_intervals()
        comparison = run_eval.compare_modes(self.results, "bm25", 2000)
        for rows in comparison["metrics"].values():
            for row in rows:
                self.assertGreaterEqual(row["holm_p"], row["p_value"])
                self.assertLessEqual(row["holm_p"], 1.0)

    def test_underpowered_comparison_is_never_marked_significant(self):
        self.with_intervals()
        comparison = run_eval.compare_modes(self.results, "bm25", 2000)
        for rows in comparison["metrics"].values():
            for row in rows:
                if row["underpowered"]:
                    self.assertFalse(row["significant"])

    def test_design_analysis_reports_the_sample_size_this_set_lacks(self):
        self.with_intervals()
        design = run_eval.design_of(self.results, "hybrid", "bm25", "mrr@10")
        self.assertEqual(design["n"], 10)
        self.assertGreater(design["min_detectable_effect"], 0.0)
        # Resolving a 0.02 MRR move needs far more than ten queries.
        self.assertGreater(design["queries_for_target"], 100)


class GatePolicyTest(HarnessTestCase):

    THRESHOLDS = {"recall@5": 0.90, "mrr@10": 0.85, "ndcg@10": 0.85}

    def test_point_policy_passes_on_the_measured_means(self):
        checks = run_eval.gate_point(self.results["hybrid"], self.THRESHOLDS)
        self.assertTrue(all(c["pass"] for c in checks), checks)

    def test_point_policy_fails_a_mode_below_the_line(self):
        checks = run_eval.gate_point(self.results["bm25"], self.THRESHOLDS)
        self.assertFalse(all(c["pass"] for c in checks))

    def test_ci_lower_policy_is_strictly_harder_than_point(self):
        # Recall@5 = 1.00 on ten queries has a lower bound of 0.69, under the 0.90 line:
        # at this sample size the interval is wider than the gate's whole safety margin,
        # which is the argument for a larger gold set rather than for a looser gate.
        self.with_intervals()
        point = run_eval.gate_point(self.results["hybrid"], self.THRESHOLDS)
        ci = run_eval.gate_ci_lower(self.results["hybrid"], self.THRESHOLDS)
        self.assertTrue(all(c["pass"] for c in point))
        self.assertFalse(all(c["pass"] for c in ci))
        recall_row = next(c for c in ci if c["metric"] == "recall@5")
        self.assertAlmostEqual(recall_row["ci_lower"], 0.025 ** 0.1, places=4)

    def test_regression_policy_passes_against_an_identical_baseline(self):
        checks = run_eval.gate_regression(self.results["hybrid"], self.results["hybrid"],
                                          self.THRESHOLDS, 2000)
        self.assertTrue(all(c["pass"] for c in checks), checks)
        for c in checks:
            self.assertEqual(c["delta"], 0.0)
            self.assertEqual(c["effective_n"], 0)

    def test_regression_policy_catches_a_drop_the_threshold_would_miss(self):
        # Degrade every query from rank 1 to rank 2: MRR@10 falls 0.95 -> 0.50, well under
        # the line, but the point of the policy is that the *paired* test fires too.
        degraded = run_eval.evaluate_mode(GOLD, "bm25")
        checks = run_eval.gate_regression(degraded, self.results["hybrid"],
                                          self.THRESHOLDS, 2000)
        mrr = next(c for c in checks if c["metric"] == "mrr@10")
        self.assertLess(mrr["delta"], 0)
        self.assertFalse(mrr["pass"])
        self.assertIn("significant drop", mrr["detail"])

    def test_regression_policy_skips_the_paired_test_on_a_mismatched_baseline(self):
        shorter = run_eval.evaluate_mode(GOLD[:6], "hybrid")
        checks = run_eval.gate_regression(self.results["hybrid"], shorter,
                                          self.THRESHOLDS, 2000)
        for c in checks:
            self.assertIn("paired test skipped", c["detail"])
            self.assertNotIn("delta", c)


class ReportRenderingTest(HarnessTestCase):

    def test_markdown_carries_intervals_significance_and_the_gate_verdict(self):
        self.with_intervals()
        comparison = run_eval.compare_modes(self.results, "bm25", 2000)
        design = run_eval.design_of(self.results, "hybrid", "bm25", "mrr@10")
        gate = run_eval.gate_point(self.results["hybrid"], GatePolicyTest.THRESHOLDS)
        md = run_eval.render_markdown(self.results, len(GOLD), gate, "hybrid", "point",
                                      comparison, design)
        for expected in ("Recall@5", "Significance vs", "Holm p",
                         "What this gold set can resolve", "Regression gate",
                         "✅ **PASS**", "First relevant rank per query"):
            self.assertIn(expected, md)

    def test_console_report_runs_clean_with_and_without_stats(self):
        self.with_intervals()
        comparison = run_eval.compare_modes(self.results, "bm25", 2000)
        design = run_eval.design_of(self.results, "hybrid", "bm25", "mrr@10")
        out = io.StringIO()
        with redirect_stdout(out):
            run_eval.print_report(self.results, len(GOLD), comparison, design)
            run_eval.print_report(self.results, len(GOLD), None, None)
        self.assertIn("paired randomization test vs bm25", out.getvalue())
        self.assertIn("resolution of this gold set", out.getvalue())

    def test_json_payload_round_trips(self):
        self.with_intervals()
        payload = {
            "queries": len(GOLD),
            "alpha": run_eval.ALPHA,
            "modes": self.results,
            "comparison": run_eval.compare_modes(self.results, "bm25", 2000),
            "design": run_eval.design_of(self.results, "hybrid", "bm25", "mrr@10"),
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with open(path, encoding="utf-8") as f:
            restored = json.load(f)
        self.assertEqual(restored["modes"]["hybrid"]["recall@5"], 1.0)
        self.assertEqual(restored["comparison"]["baseline"], "bm25")
        self.assertIn("min_detectable_effect", restored["design"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------------------
# sequential gating (docs/adr/0012)
# --------------------------------------------------------------------------------------

BIG_GOLD = [{"query": f"s{i}", "relevant_doc_ids": [f"d{i}"]} for i in range(200)]


class SequentialGateTest(unittest.TestCase):
    """Drives the early-stopping path against a backend whose quality is dialled by hand."""

    THRESHOLDS = {"recall@5": 0.90, "mrr@10": 0.85, "ndcg@10": 0.85}

    def install(self, rank_for):
        calls = []

        def stub(query, mode, attempts=3):
            index = int(query[1:])
            calls.append(index)
            rank = rank_for(index)
            ranked = [f"filler-{index}-{j}" for j in range(run_eval.RETRIEVE_K)]
            if rank is not None:
                ranked[rank - 1] = f"d{index}"
            return ranked

        real = run_eval.search
        run_eval.search = stub
        self.addCleanup(lambda: setattr(run_eval, "search", real))
        return calls

    def run_gate(self, rank_for, gold=None):
        calls = self.install(rank_for)
        result, run = run_eval.evaluate_sequential(
            gold or BIG_GOLD, "hybrid", self.THRESHOLDS, run_eval.ALPHA,
            run_eval.SEQUENTIAL_SEED)
        return result, run, calls

    def test_a_strong_system_is_cleared_without_spending_the_whole_gold_set(self):
        _, run, calls = self.run_gate(lambda i: 1)
        summary = run["summary"]
        self.assertEqual(summary["stop_reason"], "all metrics decided")
        self.assertTrue(summary["stopped_early"])
        self.assertLess(summary["queries_used"], len(BIG_GOLD))
        self.assertGreater(summary["saved_fraction"], 0.5)
        self.assertTrue(all(v["pass"] for v in run["verdicts"].values()))
        # The saving is real work not done, not a smaller number in a report.
        self.assertEqual(len(calls), summary["queries_used"])

    def test_a_broken_system_fails_faster_than_a_good_one_passes(self):
        _, good, _ = self.run_gate(lambda i: 1)
        _, bad, bad_calls = self.run_gate(lambda i: None)
        self.assertIn("failed", bad["summary"]["stop_reason"])
        self.assertLess(bad["summary"]["queries_used"], good["summary"]["queries_used"])
        self.assertEqual(len(bad_calls), bad["summary"]["queries_used"])

    def test_short_circuiting_leaves_the_other_metrics_unsettled_not_passed(self):
        _, run, _ = self.run_gate(lambda i: None)
        decisions = {m: v["decision"] for m, v in run["verdicts"].items()}
        self.assertIn("fail", decisions.values())
        for verdict in run["verdicts"].values():
            self.assertFalse(verdict["pass"])

    def test_a_system_on_the_line_exhausts_the_budget_and_does_not_pass(self):
        # Recall@5 lands at exactly the 0.90 threshold, so no evidence ever accumulates.
        _, run, _ = self.run_gate(lambda i: 1 if i % 10 else None)
        self.assertEqual(run["summary"]["stop_reason"], "budget exhausted")
        self.assertFalse(run["summary"]["stopped_early"])
        self.assertEqual(run["summary"]["saved"], 0)
        recall = run["verdicts"]["recall@5"]
        self.assertEqual(recall["decision"], "undecided")
        self.assertFalse(recall["pass"])

    def test_the_shuffle_is_seeded_so_two_runs_agree(self):
        first = self.run_gate(lambda i: 1)[1]["summary"]
        second = self.run_gate(lambda i: 1)[1]["summary"]
        self.assertEqual(first, second)

    def test_markdown_reports_what_was_saved(self):
        result, run, _ = self.run_gate(lambda i: 1)
        run_eval.add_intervals({"hybrid": result}, 500)
        md = run_eval.render_markdown({"hybrid": result}, run["summary"]["queries_used"],
                                      run_eval.gate_sequential(run), "hybrid", "sequential",
                                      None, None, run)
        self.assertIn("Stopped early", md)
        self.assertIn("query budget left unspent", md)
        self.assertIn("valid at every sample size", md)

    def test_exhausting_the_budget_is_reported_as_a_gold_set_problem(self):
        _, exhausted, _ = self.run_gate(lambda i: 1 if i % 10 else None)
        rows = run_eval.gate_sequential(exhausted)
        self.assertTrue(any("gold set too small" in r["detail"] for r in rows))
        self.assertFalse(all(r["pass"] for r in rows))


class SequentialGateWordingTest(unittest.TestCase):
    """`gate_sequential` is pure, so its three phrasings are pinned directly.

    Reaching each one through the gate itself would mean engineering a run where one
    metric fails while another is genuinely still open — possible, but it would make the
    assertion depend on where the stopping times happen to land.
    """

    def rows(self, stop_reason: str, decisions: dict[str, str], used: int = 40):
        run = {
            "summary": {"queries_used": used, "queries_available": 200,
                        "saved": 200 - used, "saved_fraction": (200 - used) / 200,
                        "stopped_early": stop_reason != "budget exhausted",
                        "stop_reason": stop_reason, "alpha": 0.05, "seed": 1},
            "verdicts": {m: {"metric": m, "threshold": 0.85, "decision": d,
                             "measured": 0.5, "pass": d == "pass"}
                         for m, d in decisions.items()},
        }
        return {r["metric"]: r for r in run_eval.gate_sequential(run)}

    def test_a_settled_metric_names_its_decision_and_cost(self):
        rows = self.rows("all metrics decided", {"mrr@10": "pass"})
        self.assertEqual(rows["mrr@10"]["detail"], "pass after 40/200 queries")
        self.assertTrue(rows["mrr@10"]["pass"])

    def test_a_short_circuited_metric_is_not_settled_rather_than_exhausted(self):
        # The build went red on another metric, so this one was never given the chance —
        # calling that "budget exhausted" would misdescribe why no answer exists.
        rows = self.rows("recall@5 failed",
                         {"recall@5": "fail", "ndcg@10": "undecided"})
        self.assertEqual(rows["recall@5"]["detail"], "fail after 40/200 queries")
        self.assertIn("not settled", rows["ndcg@10"]["detail"])
        self.assertIn("recall@5 failed", rows["ndcg@10"]["detail"])
        self.assertNotIn("gold set too small", rows["ndcg@10"]["detail"])

    def test_running_out_of_queries_blames_the_gold_set(self):
        rows = self.rows("budget exhausted", {"mrr@10": "undecided"}, used=200)
        self.assertIn("gold set too small", rows["mrr@10"]["detail"])
        self.assertFalse(rows["mrr@10"]["pass"])


class ScoreCacheTest(unittest.TestCase):
    """A sweep must not throw away the modes that already finished.

    The report is written only once every mode is done, so a failure in the last mode used
    to discard the earlier ones — at benchmark scale, hours of completed work for a fault in
    the part still running. These tests pin the resume contract by counting the searches
    actually issued, because "did it avoid the network" is the only thing that matters here.
    """

    GOLD = [{"query": f"q{i}", "relevant_doc_ids": ["D1"]} for i in range(1, 6)]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scores.json")
        self.calls: list[tuple[str, str]] = []
        real = run_eval.search

        def counting_search(query, mode, attempts=3):
            self.calls.append((mode, query))
            return ["D1", "D2", "D3"]

        run_eval.search = counting_search
        self.addCleanup(lambda: setattr(run_eval, "search", real))

    def sweep(self, modes=("bm25", "hybrid"), path=None):
        with redirect_stdout(io.StringIO()):
            cache = run_eval.ScoreCache(self.path if path is None else path)
            return {m: run_eval.evaluate_mode(self.GOLD, m, cache) for m in modes}

    def test_a_finished_mode_is_not_scored_twice(self):
        # The bug this replaces: hybrid failing discarded a completed bm25 sweep.
        self.sweep()
        with open(self.path, encoding="utf-8") as f:
            cached = json.load(f)
        cached["hybrid"] = cached["hybrid"][:2]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cached, f)

        self.calls.clear()
        self.sweep()
        self.assertEqual([m for m, _ in self.calls], ["hybrid"] * 3)

    def test_every_query_is_durable_before_the_next_one_runs(self):
        self.sweep(modes=("bm25",))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["bm25"]), len(self.GOLD))

    def test_a_complete_cache_spends_no_searches(self):
        first = self.sweep()
        self.calls.clear()
        self.assertEqual(self.sweep(), first)
        self.assertEqual(self.calls, [])

    def test_a_damaged_cache_costs_searches_rather_than_the_run(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"bm25": [{"query"')
        self.sweep(modes=("bm25",))
        self.assertEqual(len(self.calls), len(self.GOLD))

    def test_modes_do_not_read_each_others_scores(self):
        # Same query text, different mode: reuse across modes would silently equate them.
        self.sweep(modes=("bm25",))
        self.calls.clear()
        self.sweep(modes=("hybrid",))
        self.assertEqual([m for m, _ in self.calls], ["hybrid"] * len(self.GOLD))

    def test_running_without_a_cache_is_unchanged(self):
        self.assertEqual(self.sweep(path=""), self.sweep(path=""))
