"""Tests for the calibration CLI (eval/calibrate.py).

This tool writes numbers into `application.yml` that decide how often the system answers
from context it should not trust, so the tests are mostly about the ways it could produce a
confident, wrong, or useless answer: leaking the held-out split, picking a temperature that
silently destroys the benefit, or certifying a policy that never answers and calling it a
guarantee.

Everything runs against `_mock_reranker.MockSearchServer` over loopback — no stack, no
network, seeded fixtures.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import calibrate
import conformal as cf
from _mock_reranker import MockSearchServer


class CalibrationRun(unittest.TestCase):
    """Drives the whole CLI once per class and inspects the certificate it wrote."""

    QUERIES = 800
    MISS_RATE = 0.08
    ARGS: list[str] = []

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        gold = os.path.join(cls._tmp.name, "gold.jsonl")
        cls.certificate_path = os.path.join(cls._tmp.name, "cert.json")
        with MockSearchServer(queries=cls.QUERIES, miss_rate=cls.MISS_RATE) as server:
            with open(gold, "w", encoding="utf-8") as f:
                f.write("\n".join(server.gold_lines()) + "\n")
            previous = os.environ.get("RECALL_API")
            os.environ["RECALL_API"] = server.url
            calibrate.API = server.url
            try:
                sys.argv = ["calibrate.py", gold, "--json", cls.certificate_path, *cls.ARGS]
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cls.exit_code = calibrate.main()
                cls.report = buffer.getvalue()
                cls.api_calls = server.calls
            finally:
                if previous is None:
                    os.environ.pop("RECALL_API", None)
                else:
                    os.environ["RECALL_API"] = previous
        with open(cls.certificate_path, encoding="utf-8") as f:
            cls.certificate = json.load(f)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class DefaultCalibrationTest(CalibrationRun):

    def test_runs_clean_and_queries_each_gold_entry_once(self):
        self.assertEqual(self.exit_code, 0)
        self.assertEqual(self.api_calls, self.QUERIES)

    def test_the_three_splits_partition_the_gold_set(self):
        splits = self.certificate["splits"]
        self.assertEqual(splits["total"], self.QUERIES)
        self.assertEqual(splits["tuning"] + splits["calibration"] + splits["held_out"],
                         self.QUERIES)
        for name in ("tuning", "calibration", "held_out"):
            self.assertGreater(splits[name], 0, name)

    def test_coverage_is_certified_and_holds_on_data_it_never_saw(self):
        coverage = self.certificate["coverage"]
        check = self.certificate["coverage_held_out"]
        self.assertTrue(coverage["certified"])
        self.assertGreater(check["n"], 0)
        # The guarantee is marginal over calibration draws, so a single split lands in an
        # interval around 1 - alpha rather than exactly on it. The interval has to contain
        # the promise; the point estimate need not.
        self.assertGreaterEqual(check["coverage_ci"][1], 1 - coverage["alpha"])

    def test_adaptive_sizing_beats_the_fixed_k_carrying_the_same_promise(self):
        check = self.certificate["coverage_held_out"]
        self.assertLess(check["mean_k"], check["fixed_k"])
        self.assertGreater(check["context_saved_vs_fixed"], 0.15)

    def test_the_temperature_is_chosen_not_assumed(self):
        sweep = self.certificate["temperature_sweep"]
        self.assertEqual(len(sweep), len(calibrate.TEMPERATURE_GRID))
        chosen = self.certificate["temperature"]
        viable = [row for row in sweep if row["certified"]]
        self.assertEqual(chosen, min(viable, key=lambda r: r["mean_k"])["temperature"])
        # A badly chosen temperature costs the whole benefit without failing anything —
        # the grid must actually separate the good choice from the bad one.
        self.assertGreater(max(r["mean_k"] for r in viable),
                           1.5 * min(r["mean_k"] for r in viable))

    def test_recall_misses_are_excluded_rather_than_padded_over(self):
        coverage = self.certificate["coverage"]
        self.assertGreater(coverage["dropped_recall_misses"], 0)
        self.assertLess(coverage["dropped_recall_misses"], coverage["n_calibration"])

    def test_the_report_prints_pasteable_configuration(self):
        self.assertIn("recall.rag.conformal:", self.report)
        self.assertIn("enabled: true", self.report)
        self.assertIn(f"threshold: {self.certificate['coverage']['threshold']:.6f}",
                      self.report)

    def test_a_stalled_risk_walk_names_the_sample_size_that_would_settle_it(self):
        blocked = self.certificate["risk"]["blocked_by"]
        if blocked is None:
            self.skipTest("the walk reached the end of the grid on this fixture")
        self.assertLess(blocked["empirical_risk"], self.certificate["risk"]["alpha"])
        self.assertGreater(blocked["queries_needed"], blocked["n_calibration"])
        self.assertIn("calibration queries to prove", self.report)


class TightAlphaTest(CalibrationRun):
    """A stricter coverage target must buy longer contexts, not a quieter report."""

    QUERIES = 800
    ARGS = ["--alpha", "0.02"]

    def test_a_tighter_promise_costs_context(self):
        self.assertTrue(self.certificate["coverage"]["certified"])
        self.assertEqual(self.certificate["coverage"]["alpha"], 0.02)
        self.assertGreater(self.certificate["coverage_held_out"]["mean_k"], 5.0)

    def test_the_stricter_promise_still_holds_out_of_sample(self):
        check = self.certificate["coverage_held_out"]
        self.assertGreaterEqual(check["coverage_ci"][1], 0.98)


class SmallGoldSetTest(CalibrationRun):
    """The case this repo keeps running into: not enough queries to certify anything."""

    QUERIES = 60
    ARGS = ["--alpha", "0.02"]

    def test_it_refuses_rather_than_certifying_on_too_few_queries(self):
        coverage = self.certificate["coverage"]
        self.assertFalse(coverage["certified"])
        self.assertIsNone(coverage["threshold"])
        self.assertIn("NOT CERTIFIED", self.report)
        self.assertIn("minimum", self.report)

    def test_an_uncertified_run_proposes_no_configuration_change(self):
        self.assertNotIn("enabled: true", self.report)


class DegenerateCertificateTest(unittest.TestCase):
    """A guarantee that answers nothing is valid, useless, and must be labelled as such."""

    def test_abstaining_on_everything_is_flagged_degenerate(self):
        sample = [(0.4, False)] * 200
        grid = [1.0, 0.9, 0.1]
        certificate = cf.risk_controlling_threshold(
            grid,
            lambda t: (sum(1 for s, ok in sample if s >= t and not ok) / len(sample),
                       len(sample)),
            alpha=0.05, delta=0.05,
            abstention_fn=lambda t: sum(1 for s, _ in sample if s < t) / len(sample))
        self.assertTrue(certificate.certified)
        self.assertTrue(certificate.degenerate)
        self.assertEqual(certificate.empirical_abstention, 1.0)

    def test_a_working_policy_is_not_flagged(self):
        sample = [(0.9, True)] * 190 + [(0.1, False)] * 10
        certificate = cf.risk_controlling_threshold(
            [1.0, 0.5, 0.05],
            lambda t: (sum(1 for s, ok in sample if s >= t and not ok) / len(sample),
                       len(sample)),
            alpha=0.05, delta=0.05,
            abstention_fn=lambda t: sum(1 for s, _ in sample if s < t) / len(sample))
        self.assertTrue(certificate.certified)
        self.assertFalse(certificate.degenerate)
        self.assertLess(certificate.empirical_abstention, 0.2)


class QueriesForRiskBoundTest(unittest.TestCase):

    def test_inverts_the_hoeffding_bound(self):
        needed = cf.queries_for_risk_bound(0.02, 0.05, 0.05)
        self.assertIsNotNone(needed)
        # At exactly that n the bound should clear delta; a hair under, it should not.
        self.assertLessEqual(cf.hoeffding_bentkus_p(0.02, needed, 0.05), 0.05)
        self.assertGreater(cf.hoeffding_bentkus_p(0.02, max(1, needed // 3), 0.05), 0.05)

    def test_a_narrower_margin_needs_more_data(self):
        self.assertGreater(cf.queries_for_risk_bound(0.045, 0.05, 0.05),
                           cf.queries_for_risk_bound(0.005, 0.05, 0.05))

    def test_no_sample_size_helps_once_the_risk_meets_the_target(self):
        self.assertIsNone(cf.queries_for_risk_bound(0.05, 0.05, 0.05))
        self.assertIsNone(cf.queries_for_risk_bound(0.20, 0.05, 0.05))


if __name__ == "__main__":
    unittest.main(verbosity=2)
