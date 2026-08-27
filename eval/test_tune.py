"""Tests for the self-tuning sweep's guards (eval/tune.py).

The tuner can rewrite `application.yml` and open a pull request without a human in the
loop, so the interesting tests are the ones that try to make it fire when it should not.
Everything here is the decision logic — splitting and the four guards — driven directly
with synthetic per-query scores, so no stack and no network are needed.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import unittest

import tune

KOREAN = "메시지가 중복 저장되는 걸 막으려면"
GOLD = ([{"query": f"english query {i}", "relevant_doc_ids": [f"d{i}"]} for i in range(8)]
        + [{"query": f"{KOREAN} {i}", "relevant_doc_ids": [f"k{i}"]} for i in range(4)])


def config(rrf_k: int, candidates: int, rrs: list[float]) -> dict:
    """A grid point with hand-chosen per-query reciprocal ranks."""
    return {"rrf_k": rrf_k, "candidates": candidates,
            "recall5": 0.0, "mrr10": sum(rrs) / len(rrs), "ndcg10": 0.0,
            "per_query": {"recall5": rrs, "rr": rrs, "ndcg": rrs}}


class SplitTest(unittest.TestCase):

    def test_split_is_exhaustive_and_disjoint(self):
        dev, test = tune.split_gold(GOLD, 0.5)
        self.assertEqual(len(dev) + len(test), len(GOLD))
        dev_q = {e["query"] for e in dev}
        test_q = {e["query"] for e in test}
        self.assertEqual(dev_q & test_q, set())
        self.assertEqual(dev_q | test_q, {e["query"] for e in GOLD})

    def test_split_is_deterministic_across_runs(self):
        first = tune.split_gold(GOLD, 0.5)
        second = tune.split_gold(GOLD, 0.5)
        self.assertEqual([e["query"] for e in first[0]], [e["query"] for e in second[0]])

    def test_a_different_seed_gives_a_different_split(self):
        default = {e["query"] for e in tune.split_gold(GOLD, 0.5)[0]}
        other = {e["query"] for e in tune.split_gold(GOLD, 0.5, seed=99)[0]}
        self.assertNotEqual(default, other)

    def test_korean_queries_land_on_both_sides(self):
        # Cross-lingual queries are the ones a fusion knob actually moves; quarantining
        # them in one split would tune on a different problem than it validates on.
        dev, test = tune.split_gold(GOLD, 0.5)
        self.assertTrue(any(tune.stratum_of(e) == "ko" for e in dev))
        self.assertTrue(any(tune.stratum_of(e) == "ko" for e in test))

    def test_explicit_lang_field_overrides_script_detection(self):
        self.assertEqual(tune.stratum_of({"query": "hello", "lang": "ko"}), "ko")
        self.assertEqual(tune.stratum_of({"query": KOREAN}), "ko")
        self.assertEqual(tune.stratum_of({"query": "plain english"}), "en")

    def test_dev_fraction_is_honoured(self):
        dev, test = tune.split_gold(GOLD, 0.75)
        self.assertGreater(len(dev), len(test))


class GuardTest(unittest.TestCase):

    def decide(self, dev_rrs, base_rrs, test_rrs=None, test_base_rrs=None,
               epsilon=0.01, grid_noise=(0.5, 0.5)):
        base = config(60, 50, base_rrs)
        best = config(20, 100, dev_rrs)
        others = [config(120, 25 + i, [r] * len(base_rrs)) for i, r in enumerate(grid_noise)]
        grid = [base, best, *others]
        test_base = config(60, 50, test_base_rrs) if test_base_rrs else None
        test_best = config(20, 100, test_rrs) if test_rrs else None
        return tune.decide(grid, base, best, test_base, test_best, epsilon)

    def guard(self, decision, name):
        return next(g for g in decision["guards"] if g["name"] == name)

    def test_a_real_uniform_improvement_that_generalises_is_proposed(self):
        # Every dev query improves (so the paired test bottoms out at its floor) and the
        # held-out split moves the same way.
        d = self.decide(dev_rrs=[1.0] * 10, base_rrs=[0.5] * 10,
                        test_rrs=[1.0] * 8, test_base_rrs=[0.5] * 8)
        self.assertTrue(d["improved"], d["guards"])
        for name in ("effect size", "evidence on dev", "generalisation"):
            self.assertTrue(self.guard(d, name)["pass"])

    def test_a_lift_carried_by_too_few_queries_is_refused(self):
        # +0.30 MRR on the mean, but only three of ten queries differ: floor p is 0.25,
        # so no outcome could have been significant. This is the shipped gold set's shape.
        dev = [1.0, 1.0, 1.0] + [0.5] * 7
        base = [0.0, 0.0, 0.0] + [0.5] * 7
        d = self.decide(dev_rrs=dev, base_rrs=base, test_rrs=[1.0] * 8,
                        test_base_rrs=[0.5] * 8)
        self.assertFalse(d["improved"])
        self.assertTrue(self.guard(d, "effect size")["pass"])
        self.assertFalse(self.guard(d, "evidence on dev")["pass"])
        self.assertEqual(d["dev_effective_n"], 3)

    def test_an_improvement_below_epsilon_is_refused_however_clean(self):
        dev = [0.501] * 10
        base = [0.500] * 10
        d = self.decide(dev_rrs=dev, base_rrs=base, test_rrs=[0.501] * 8,
                        test_base_rrs=[0.500] * 8, epsilon=0.01)
        self.assertFalse(d["improved"])
        self.assertFalse(self.guard(d, "effect size")["pass"])

    def test_a_dev_win_that_reverses_on_the_held_out_split_is_refused(self):
        # The failure mode the split exists to catch: the grid found a configuration that
        # fits the dev queries and does worse on ones it never saw.
        d = self.decide(dev_rrs=[1.0] * 10, base_rrs=[0.5] * 10,
                        test_rrs=[0.3] * 8, test_base_rrs=[0.5] * 8)
        self.assertFalse(d["improved"])
        self.assertTrue(self.guard(d, "evidence on dev")["pass"])
        self.assertFalse(self.guard(d, "generalisation")["pass"])
        self.assertLess(d["held_out"]["delta"], 0)
        self.assertGreater(d["held_out"]["gap"], 0)      # dev lift minus held-out lift

    def test_without_a_held_out_split_nothing_is_proposed(self):
        d = self.decide(dev_rrs=[1.0] * 10, base_rrs=[0.5] * 10)
        self.assertFalse(d["improved"])
        self.assertFalse(self.guard(d, "generalisation")["pass"])
        self.assertIsNone(d["held_out"])

    def test_every_guard_is_reported_even_after_one_fails(self):
        # A report that stops at the first failure hides whether the gold set or the
        # change is the problem.
        d = self.decide(dev_rrs=[0.5001] * 10, base_rrs=[0.5] * 10,
                        test_rrs=[0.9] * 8, test_base_rrs=[0.5] * 8)
        self.assertEqual(len(d["guards"]), 3)
        self.assertFalse(self.guard(d, "effect size")["pass"])
        self.assertTrue(self.guard(d, "generalisation")["pass"])

    def test_holm_correction_spans_the_whole_grid(self):
        d = self.decide(dev_rrs=[1.0] * 10, base_rrs=[0.5] * 10,
                        test_rrs=[1.0] * 8, test_base_rrs=[0.5] * 8)
        self.assertEqual(len(d["grid_p"]), 4)            # baseline + winner + 2 fillers
        self.assertGreaterEqual(d["dev_holm_p"], d["dev_p"])

    def test_design_block_names_the_sample_size_that_would_settle_it(self):
        d = self.decide(dev_rrs=[1.0, 1.0, 1.0] + [0.5] * 7, base_rrs=[0.0] * 3 + [0.5] * 7,
                        test_rrs=[1.0] * 8, test_base_rrs=[0.5] * 8)
        self.assertIn("queries_for_target", d["design"])
        self.assertGreater(d["design"]["queries_for_target"], 10)


class ReportTest(unittest.TestCase):

    def build(self, improved: bool):
        base = config(60, 50, [0.5] * 10)
        best = (config(20, 100, [1.0] * 10) if improved
                else config(20, 100, [1.0, 1.0, 1.0] + [0.5] * 7))
        base_rrs = [0.5] * 10 if improved else [0.0] * 3 + [0.5] * 7
        base = config(60, 50, base_rrs)
        grid = [base, best]
        decision = tune.decide(grid, base, best,
                               config(60, 50, [0.5] * 8), config(20, 100, [1.0] * 8), 0.01)
        return tune.render_report(grid, base, best, decision, 10, 8), decision

    def test_report_shows_the_guard_table_and_the_split_sizes(self):
        md, decision = self.build(improved=True)
        self.assertIn("### Guards", md)
        self.assertIn("10-query dev split", md)
        self.assertIn("8-query held-out", md)
        self.assertIn("Holm p", md)
        self.assertTrue(decision["improved"])
        self.assertIn("All guards pass", md)

    def test_a_refused_run_says_how_many_queries_would_settle_it(self):
        md, decision = self.build(improved=False)
        self.assertFalse(decision["improved"])
        self.assertIn("**No proposal**", md)
        self.assertIn("queries** — against", md)
        self.assertIn("bigger benchmark, not a smaller epsilon", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
