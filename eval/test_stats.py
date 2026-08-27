"""Tests for the statistical inference layer (eval/stats.py).

Three kinds of check, in decreasing order of how much they prove:

1. **Against closed forms.** Clopper-Pearson at k = n has the analytic lower bound
   (alpha/2)^(1/n); the sign-flip test on all-positive differences has the analytic
   p-value 2^(1-k); Holm reproduces a worked textbook example. These pin exact numbers.
2. **Against an independent implementation.** The permutation test is re-derived by
   brute force inside the test, and the special functions are compared with SciPy when
   SciPy is installed (it is not a dependency of the harness — the check skips without it).
3. **By simulation.** The bootstrap interval is checked for actual coverage against a
   known population, which is the only way to catch a subtly wrong BCa correction.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import math
import random
import unittest

import stats

try:                                    # optional cross-check, never required
    from scipy import special as _scipy_special
    from scipy import stats as _scipy_stats
    HAVE_SCIPY = True
except ImportError:                     # pragma: no cover - depends on the environment
    HAVE_SCIPY = False


class IncompleteBetaTest(unittest.TestCase):
    """The one special function we implement by hand, so it gets checked hard."""

    def test_boundaries(self):
        self.assertEqual(stats.betainc(2.0, 3.0, 0.0), 0.0)
        self.assertEqual(stats.betainc(2.0, 3.0, 1.0), 1.0)

    def test_symmetry_identity(self):
        # I_x(a, b) == 1 - I_{1-x}(b, a) holds for every a, b, x.
        for a, b, x in [(0.5, 0.5, 0.3), (2.0, 3.0, 0.7), (12.0, 4.0, 0.55), (1.0, 30.0, 0.02)]:
            self.assertAlmostEqual(stats.betainc(a, b, x),
                                   1.0 - stats.betainc(b, a, 1.0 - x), places=12)

    def test_uniform_special_case(self):
        # Beta(1, 1) is Uniform(0, 1), so its CDF is the identity.
        for x in (0.01, 0.25, 0.5, 0.9, 0.999):
            self.assertAlmostEqual(stats.betainc(1.0, 1.0, x), x, places=12)

    def test_ppf_inverts_cdf(self):
        for a, b in [(1.0, 1.0), (2.0, 5.0), (10.0, 1.0), (0.5, 0.5), (30.0, 70.0)]:
            for p in (0.025, 0.5, 0.975):
                x = stats.beta_ppf(p, a, b)
                self.assertAlmostEqual(stats.betainc(a, b, x), p, places=9)

    @unittest.skipUnless(HAVE_SCIPY, "SciPy not installed")
    def test_matches_scipy(self):
        for a, b, x in [(0.5, 0.5, 0.3), (2.0, 3.0, 0.7), (12.0, 4.0, 0.55),
                        (1.0, 30.0, 0.02), (100.0, 100.0, 0.48)]:
            self.assertAlmostEqual(stats.betainc(a, b, x),
                                   float(_scipy_special.betainc(a, b, x)), places=10)


class ClopperPearsonTest(unittest.TestCase):

    def test_all_successes_matches_closed_form(self):
        # At k = n the exact lower bound is (alpha/2)^(1/n) — the number that makes
        # "Recall@5 = 1.00 over 10 queries" an honest claim rather than a perfect one.
        lo, hi = stats.clopper_pearson(10, 10, alpha=0.05)
        self.assertAlmostEqual(lo, 0.025 ** (1 / 10), places=9)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 0.6915, places=4)

    def test_no_successes_is_mirrored(self):
        lo, hi = stats.clopper_pearson(0, 10, alpha=0.05)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 1.0 - 0.025 ** (1 / 10), places=9)

    def test_interval_brackets_the_estimate_and_widens_with_smaller_n(self):
        for n in (10, 100, 1000):
            lo, hi = stats.clopper_pearson(int(0.7 * n), n)
            self.assertLess(lo, 0.7)
            self.assertGreater(hi, 0.7)
        narrow = stats.clopper_pearson(700, 1000)
        wide = stats.clopper_pearson(7, 10)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_rejects_impossible_counts(self):
        with self.assertRaises(ValueError):
            stats.clopper_pearson(11, 10)

    @unittest.skipUnless(HAVE_SCIPY, "SciPy not installed")
    def test_matches_scipy_binomtest(self):
        for k, n in [(0, 5), (1, 5), (7, 10), (10, 10), (23, 40), (300, 1000)]:
            expected = _scipy_stats.binomtest(k, n).proportion_ci(
                confidence_level=0.95, method="exact")
            lo, hi = stats.clopper_pearson(k, n)
            self.assertAlmostEqual(lo, expected.low, places=9)
            self.assertAlmostEqual(hi, expected.high, places=9)


def _brute_force_permutation_p(a, b):
    """Independent reference: enumerate every sign assignment, no shortcuts."""
    diffs = [x - y for x, y in zip(a, b)]
    nonzero = [d for d in diffs if d != 0.0]
    if not nonzero:
        return 1.0
    target = abs(sum(nonzero))
    total = extreme = 0
    for bits in range(2 ** len(nonzero)):
        signed = sum(d if (bits >> i) & 1 else -d for i, d in enumerate(nonzero))
        total += 1
        if abs(signed) >= target - 1e-12:
            extreme += 1
    return extreme / total


class PairedPermutationTest(unittest.TestCase):

    def test_uniform_improvement_hits_the_analytic_floor(self):
        # Every one of k queries improves => the observed assignment and its mirror are
        # the only extreme ones => p = 2 / 2^k exactly, which is also the attainable floor.
        for k in (3, 5, 8):
            a = [1.0] * k
            b = [0.0] * k
            r = stats.paired_permutation_test(a, b)
            self.assertTrue(r.exact)
            self.assertAlmostEqual(r.p_value, 2.0 ** (1 - k), places=12)
            self.assertAlmostEqual(r.p_value, r.min_attainable_p, places=12)

    def test_three_differing_queries_can_never_reach_significance(self):
        # The critique this module exists to make: seven ties and three wins is not
        # evidence at alpha = 0.05, however large the three wins are.
        a = [1.0, 1.0, 1.0] + [0.5] * 7
        b = [0.0, 0.0, 0.0] + [0.5] * 7
        r = stats.paired_permutation_test(a, b)
        self.assertEqual(r.effective_n, 3)
        self.assertEqual(r.n_pairs, 10)
        self.assertAlmostEqual(r.min_attainable_p, 0.25, places=12)
        self.assertTrue(r.underpowered)
        self.assertFalse(r.significant)

    def test_identical_systems_give_p_one(self):
        xs = [0.3, 0.9, 0.5]
        r = stats.paired_permutation_test(xs, list(xs))
        self.assertEqual(r.p_value, 1.0)
        self.assertEqual(r.effective_n, 0)
        self.assertEqual(r.observed_diff, 0.0)

    def test_matches_brute_force_on_random_inputs(self):
        rng = random.Random(7)
        for _ in range(40):
            n = rng.randint(2, 9)
            a = [rng.choice([0.0, 0.2, 0.5, 1.0]) for _ in range(n)]
            b = [rng.choice([0.0, 0.2, 0.5, 1.0]) for _ in range(n)]
            got = stats.paired_permutation_test(a, b)
            self.assertTrue(got.exact)
            self.assertAlmostEqual(got.p_value, _brute_force_permutation_p(a, b), places=12)

    def test_symmetric_in_its_arguments(self):
        a = [1.0, 0.5, 0.0, 0.33, 1.0]
        b = [0.5, 0.5, 0.25, 1.0, 0.2]
        self.assertAlmostEqual(stats.paired_permutation_test(a, b).p_value,
                               stats.paired_permutation_test(b, a).p_value, places=12)

    def test_monte_carlo_path_is_deterministic_and_close_to_exact(self):
        rng = random.Random(11)
        n = 24                                   # above EXACT_PERMUTATION_LIMIT
        a = [rng.random() for _ in range(n)]
        b = [x - 0.25 + rng.random() * 0.1 for x in a]
        first = stats.paired_permutation_test(a, b, iters=4000)
        second = stats.paired_permutation_test(a, b, iters=4000)
        self.assertFalse(first.exact)
        self.assertEqual(first.p_value, second.p_value)      # seeded: no CI flap
        self.assertGreater(first.p_value, 0.0)               # add-one estimator
        exactish = stats.paired_permutation_test(a, b, iters=4000, exact_limit=n)
        self.assertTrue(exactish.exact)
        self.assertLess(abs(first.p_value - exactish.p_value), 0.02)

    def test_rejects_unpaired_input(self):
        with self.assertRaises(ValueError):
            stats.paired_permutation_test([1.0, 2.0], [1.0])


class HolmTest(unittest.TestCase):

    def test_worked_example(self):
        # Matches R's p.adjust(c(0.01, 0.04, 0.03), method = "holm").
        self.assertEqual([round(p, 10) for p in stats.holm([0.01, 0.04, 0.03])],
                         [0.03, 0.06, 0.06])

    def test_preserves_input_order_and_is_monotone_in_rank(self):
        raw = [0.2, 0.001, 0.049, 0.6]
        adjusted = stats.holm(raw)
        self.assertEqual(len(adjusted), len(raw))
        by_rank = [adjusted[i] for i in sorted(range(len(raw)), key=lambda i: raw[i])]
        self.assertEqual(by_rank, sorted(by_rank))

    def test_never_loosens_a_p_value_and_stays_bounded(self):
        raw = [0.01, 0.02, 0.5, 0.9]
        for original, adjusted in zip(raw, stats.holm(raw)):
            self.assertGreaterEqual(adjusted, original)
            self.assertLessEqual(adjusted, 1.0)

    def test_single_test_is_unchanged(self):
        self.assertEqual(stats.holm([0.031]), [0.031])

    def test_empty(self):
        self.assertEqual(stats.holm([]), [])


class BootstrapTest(unittest.TestCase):

    def test_degenerate_sample_collapses(self):
        # Every query scored 1.0: resampling can only ever produce 1.0. The interval is
        # a point, which is precisely why proportions go through clopper_pearson instead.
        self.assertEqual(stats.bootstrap_ci([1.0] * 10), (1.0, 1.0))

    def test_brackets_the_observed_mean(self):
        values = [0.1, 0.9, 0.5, 0.33, 1.0, 0.2, 0.66, 0.25]
        lo, hi = stats.bootstrap_ci(values, iters=4000)
        self.assertLess(lo, stats.mean(values))
        self.assertGreater(hi, stats.mean(values))

    def test_deterministic_under_the_default_seed(self):
        values = [0.1, 0.9, 0.5, 0.33, 1.0, 0.2, 0.66, 0.25]
        self.assertEqual(stats.bootstrap_ci(values, iters=2000),
                         stats.bootstrap_ci(values, iters=2000))

    def test_interval_narrows_as_the_sample_grows(self):
        rng = random.Random(3)
        small = [rng.random() for _ in range(10)]
        large = [rng.random() for _ in range(400)]
        width = lambda ci: ci[1] - ci[0]                                  # noqa: E731
        self.assertLess(width(stats.bootstrap_ci(large, iters=2000)),
                        width(stats.bootstrap_ci(small, iters=2000)))

    def test_empirical_coverage_is_near_nominal(self):
        # The real test of a BCa implementation: over many samples from a known
        # population, a 95% interval should contain the true mean about 95% of the time.
        # Beta(2, 5) is skewed, which is where a plain percentile interval degrades.
        rng = random.Random(2024)
        true_mean = 2 / (2 + 5)
        replications, n, covered = 300, 40, 0
        for i in range(replications):
            sample = [rng.betavariate(2, 5) for _ in range(n)]
            lo, hi = stats.bootstrap_ci(sample, iters=600, seed=1000 + i)
            covered += lo <= true_mean <= hi
        self.assertGreater(covered / replications, 0.88)
        self.assertLess(covered / replications, 0.995)


class DesignAnalysisTest(unittest.TestCase):

    def test_min_attainable_p_is_two_over_two_to_the_k(self):
        self.assertEqual(stats.min_attainable_p(0), 1.0)
        self.assertEqual(stats.min_attainable_p(1), 1.0)
        self.assertAlmostEqual(stats.min_attainable_p(2), 0.5, places=12)
        self.assertAlmostEqual(stats.min_attainable_p(10), 2 / 1024, places=12)

    def test_a_ten_query_gold_set_can_reach_significance_at_all(self):
        # 2/2^10 = 0.00195 < 0.05, so ten fully-differing queries is enough in principle —
        # the constraint in practice is ties, not the sample size alone.
        self.assertLess(stats.min_attainable_p(10), 0.05)
        self.assertGreater(stats.min_attainable_p(5), 0.05)

    def test_required_queries_matches_the_closed_form(self):
        z = stats._NORM.inv_cdf(0.975) + stats._NORM.inv_cdf(0.80)
        expected = math.ceil((z * 0.3 / 0.05) ** 2)
        self.assertEqual(stats.required_queries(0.05, 0.3), expected)

    def test_required_queries_shrinks_as_the_effect_grows(self):
        self.assertLess(stats.required_queries(0.20, 0.3), stats.required_queries(0.05, 0.3))

    def test_required_queries_undefined_without_an_effect(self):
        self.assertIsNone(stats.required_queries(0.0, 0.3))
        self.assertIsNone(stats.required_queries(0.1, 0.0))

    def test_mde_falls_with_sample_size(self):
        rng = random.Random(5)
        pattern = [rng.gauss(0.1, 0.3) for _ in range(20)]
        small = stats.design_analysis(pattern, [0.0] * 20)
        big = stats.design_analysis(pattern * 10, [0.0] * 200)
        self.assertLess(big.min_detectable_effect, small.min_detectable_effect)

    def test_reports_the_sample_size_a_target_effect_would_need(self):
        rng = random.Random(9)
        a = [rng.random() for _ in range(10)]
        b = [rng.random() for _ in range(10)]
        d = stats.design_analysis(a, b, target_effect=0.02)
        self.assertEqual(d.n, 10)
        self.assertGreater(d.queries_for_target, 10)         # 0.02 is far below this n's MDE
        self.assertGreater(d.min_detectable_effect, 0.02)
        self.assertIn("min_detectable_effect", d.as_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
