"""Tests for distribution-free risk control (eval/conformal.py).

A guarantee is either kept or it is decoration, and the only way to tell is to run the
whole calibrate-then-deploy cycle many times and count the failures. That is what the two
simulation tests here do: fit a threshold on one sample, measure on a fresh one, repeat,
and check the realised error rate against what was promised. Everything else — the
quantile correction, the concentration bound, the fixed-sequence walk — is checked against
its closed form, because a guarantee that passes simulation for the wrong reason is worse
than one that fails.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import math
import random
import unittest

import conformal as cf

try:                                    # optional cross-check, never required
    from scipy import stats as _scipy_stats
    HAVE_SCIPY = True
except ImportError:                     # pragma: no cover - depends on the environment
    HAVE_SCIPY = False


# --------------------------------------------------------------------------------------
# simulators
# --------------------------------------------------------------------------------------

def make_retrieval_query(rng: random.Random, n_candidates: int = 20,
                         informative: bool = True) -> tuple[list[float], int]:
    """Reranker scores and the index of the first relevant document.

    Two populations, because that is the shape retrieval actually has: most queries have
    one obviously-right passage and a sharply peaked score profile, and a minority are
    ambiguous, with the answer buried and the scores nearly flat. A fixed top-K serves
    neither well.

    With `informative=False` the scores are shuffled free of the label, modelling a
    reranker that has learned nothing. Coverage must survive that — a conformal guarantee
    is about the calibration procedure, not about the model being any good.
    """
    ambiguous = rng.random() < 0.35
    if ambiguous:
        relevant = rng.randint(1, 10)
        peakedness = rng.uniform(0.05, 0.4)
    else:
        relevant = 0 if rng.random() < 0.85 else 1
        peakedness = rng.uniform(1.5, 4.0)
    scores = sorted((rng.gauss(0.0, 1.0) for _ in range(n_candidates)), reverse=True)
    scores = [s * peakedness for s in scores]
    if not informative:
        rng.shuffle(scores)
        relevant = rng.randrange(n_candidates)
    return scores, relevant


def make_sufficiency_query(rng: random.Random) -> tuple[float, bool]:
    """A retrieval-confidence score and whether the context could really answer the question.

    The score is informative but far from perfect, which is the realistic case and the one
    where a hand-picked threshold goes wrong quietly.
    """
    sufficient = rng.random() < 0.6
    score = rng.betavariate(5, 2) if sufficient else rng.betavariate(2, 5)
    return score, sufficient


def sufficiency_risk(sample: list[tuple[float, bool]], threshold: float) -> tuple[float, int]:
    """Share of queries answered from insufficient context at this threshold."""
    if not sample:
        return 0.0, 0
    answered_wrongly = sum(1 for score, sufficient in sample
                           if score >= threshold and not sufficient)
    return answered_wrongly / len(sample), len(sample)


# --------------------------------------------------------------------------------------
# the quantile correction
# --------------------------------------------------------------------------------------

class ConformalQuantileTest(unittest.TestCase):

    def test_uses_the_n_plus_one_corrected_rank(self):
        scores = [float(i) for i in range(1, 11)]        # 1..10
        # ceil(11 * 0.9) = 10 -> the 10th smallest, which is 10.0. The uncorrected 90th
        # percentile would have been 9.0, and that one-rank difference is the guarantee.
        self.assertEqual(cf.conformal_quantile(scores, 0.10), 10.0)
        self.assertEqual(cf.conformal_quantile(scores, 0.30), 8.0)   # ceil(11*0.7) = 8

    def test_is_order_independent(self):
        scores = [0.3, 0.9, 0.1, 0.5]
        shuffled = [0.5, 0.1, 0.9, 0.3]
        self.assertEqual(cf.conformal_quantile(scores, 0.25),
                         cf.conformal_quantile(shuffled, 0.25))

    def test_refuses_to_certify_what_the_sample_cannot_support(self):
        # ceil(11 * 0.99) = 11 > 10: ten points cannot promise 99% coverage, and saying
        # so beats returning the maximum and calling it a guarantee.
        self.assertEqual(cf.conformal_quantile([float(i) for i in range(10)], 0.01), math.inf)
        self.assertEqual(cf.conformal_quantile([], 0.1), math.inf)

    def test_minimum_calibration_size_matches_the_rank_condition(self):
        for alpha in (0.01, 0.05, 0.1, 0.2):
            n = cf.minimum_calibration_size(alpha)
            self.assertTrue(math.isfinite(cf.conformal_quantile([0.0] * n, alpha)))
            self.assertEqual(cf.conformal_quantile([0.0] * (n - 1), alpha), math.inf)

    def test_rejects_an_impossible_alpha(self):
        with self.assertRaises(ValueError):
            cf.conformal_quantile([0.1], 0.0)
        with self.assertRaises(ValueError):
            cf.conformal_quantile([0.1], 1.0)


# --------------------------------------------------------------------------------------
# adaptive set sizing
# --------------------------------------------------------------------------------------

class AdaptiveSetSizerTest(unittest.TestCase):

    def fit(self, alpha=0.10, n=500, seed=1, informative=True, **kwargs):
        rng = random.Random(seed)
        examples = [make_retrieval_query(rng, informative=informative) for _ in range(n)]
        sizer = cf.AdaptiveSetSizer(alpha=alpha, **kwargs)
        sizer.calibrate(examples)
        return sizer, rng

    def test_nonconformity_is_zero_when_the_answer_is_ranked_first(self):
        sizer = cf.AdaptiveSetSizer()
        self.assertEqual(sizer.nonconformity([5.0, 1.0, 0.0], 0), 0.0)
        self.assertGreater(sizer.nonconformity([5.0, 1.0, 0.0], 2), 0.0)

    def test_nonconformity_grows_with_the_mass_ranked_above_the_answer(self):
        sizer = cf.AdaptiveSetSizer()
        peaked = [8.0, 0.0, 0.0, 0.0]
        flat = [0.1, 0.0, 0.0, 0.0]
        # Same rank, but the peaked reranker put far more mass on the wrong passage above.
        self.assertGreater(sizer.nonconformity(peaked, 1), sizer.nonconformity(flat, 1))

    def test_nonconformity_rejects_an_index_outside_the_candidates(self):
        with self.assertRaises(ValueError):
            cf.AdaptiveSetSizer().nonconformity([1.0, 2.0], 5)

    def test_a_set_is_never_empty(self):
        sizer, _ = self.fit()
        self.assertGreaterEqual(sizer.size([0.0] * 20), 1)
        self.assertEqual(sizer.size([]), 0)

    def test_sizing_before_calibration_is_an_error(self):
        with self.assertRaises(RuntimeError):
            cf.AdaptiveSetSizer().size([1.0, 2.0])

    def test_an_ambiguous_query_gets_a_longer_context_than_a_clear_one(self):
        # The behaviour a fixed K cannot have: spend context where the model is unsure.
        sizer, _ = self.fit()
        clear = [10.0, 1.0, 0.5] + [0.0] * 17
        ambiguous = [0.2, 0.19, 0.18, 0.17] + [0.16] * 16
        self.assertLess(sizer.size(clear), sizer.size(ambiguous))

    def test_empirical_coverage_meets_the_guarantee(self):
        # Fit on one sample, measure on a fresh one, repeat. This is the claim.
        alpha, trials, covered, total = 0.10, 60, 0, 0
        for trial in range(trials):
            sizer, rng = self.fit(alpha=alpha, n=300, seed=500 + trial)
            for _ in range(120):
                scores, relevant = make_retrieval_query(rng)
                covered += relevant < sizer.size(scores)
                total += 1
        self.assertGreaterEqual(covered / total, 1 - alpha - 0.02,
                                f"coverage {covered / total:.3f} below 1 - alpha")

    def test_coverage_survives_a_reranker_that_has_learned_nothing(self):
        # The distribution-free part: the guarantee is a property of the calibration
        # procedure, not of the model. A useless reranker gets huge sets, not broken
        # coverage — the cost shows up as context length, never as a silent miss.
        alpha, trials, covered, total = 0.10, 40, 0, 0
        sizes = []
        for trial in range(trials):
            sizer, rng = self.fit(alpha=alpha, n=300, seed=900 + trial, informative=False)
            for _ in range(120):
                scores, relevant = make_retrieval_query(rng, informative=False)
                k = sizer.size(scores)
                sizes.append(k)
                covered += relevant < k
                total += 1
        self.assertGreaterEqual(covered / total, 1 - alpha - 0.02)
        self.assertGreater(sum(sizes) / len(sizes), 10)        # pays in context, not misses

    def test_adaptive_sets_are_shorter_than_the_fixed_k_with_the_same_guarantee(self):
        # The payoff, measured against the fair comparison: a *conformal* fixed K, chosen
        # by the same corrected quantile, so both carry the identical coverage promise and
        # the only difference is whether the budget can move between queries.
        rng = random.Random(77)
        calibration = [make_retrieval_query(rng) for _ in range(600)]
        alpha = 0.10

        sizer = cf.AdaptiveSetSizer(alpha=alpha)
        sizer.calibrate(calibration)
        fixed_k = cf.conformal_quantile([relevant + 1 for _, relevant in calibration], alpha)

        held_out = [make_retrieval_query(rng) for _ in range(1500)]
        adaptive_sizes = [sizer.size(scores) for scores, _ in held_out]
        adaptive_mean = sum(adaptive_sizes) / len(adaptive_sizes)
        adaptive_coverage = sum(1 for (s, r), k in zip(held_out, adaptive_sizes) if r < k)
        fixed_coverage = sum(1 for _, r in held_out if r < fixed_k)

        self.assertGreaterEqual(adaptive_coverage / len(held_out), 1 - alpha - 0.02)
        self.assertGreaterEqual(fixed_coverage / len(held_out), 1 - alpha - 0.02)
        self.assertLess(adaptive_mean, fixed_k,
                        f"adaptive mean {adaptive_mean:.2f} did not beat fixed K {fixed_k}")

    def test_calibration_report_carries_the_evidence(self):
        sizer, _ = self.fit(n=400)
        report = sizer.calibration.as_dict()
        self.assertTrue(report["certified"])
        self.assertTrue(report["guarantee_intact"])
        self.assertEqual(report["cap_binds"], 0.0)
        self.assertEqual(report["n_calibration"], 400)
        self.assertGreaterEqual(report["coverage_on_calibration"], 0.9 - 0.02)

    def test_a_hard_cap_is_reported_as_the_guarantee_being_bounded_by_the_cap(self):
        sizer, _ = self.fit(n=400, max_k=2)
        report = sizer.calibration.as_dict()
        self.assertTrue(report["certified"])
        self.assertGreater(report["cap_binds"], 0.0)
        self.assertFalse(report["guarantee_intact"])
        self.assertLessEqual(report["mean_k_on_calibration"], 2.0)

    def test_too_small_a_calibration_set_falls_back_to_every_candidate(self):
        rng = random.Random(3)
        sizer = cf.AdaptiveSetSizer(alpha=0.01)
        sizer.calibrate([make_retrieval_query(rng) for _ in range(20)])
        self.assertFalse(sizer.calibration.certified)
        self.assertEqual(sizer.size([1.0] * 20), 20)          # no certificate, no truncation

    def test_a_loaded_threshold_reproduces_the_fitted_one(self):
        sizer, _ = self.fit(n=300)
        serving = cf.AdaptiveSetSizer().load(sizer.calibration.threshold,
                                             alpha=sizer.alpha,
                                             temperature=sizer.temperature)
        scores = [3.0, 2.0, 1.5, 1.0, 0.5] + [0.0] * 15
        self.assertEqual(serving.size(scores), sizer.size(scores))

    def test_a_lower_alpha_never_shrinks_the_sets(self):
        rng = random.Random(21)
        calibration = [make_retrieval_query(rng) for _ in range(400)]
        scores = [2.0, 1.6, 1.2, 0.9, 0.5] + [0.1] * 15
        sizes = []
        for alpha in (0.20, 0.10, 0.05):
            sizer = cf.AdaptiveSetSizer(alpha=alpha)
            sizer.calibrate(calibration)
            sizes.append(sizer.size(scores))
        self.assertEqual(sizes, sorted(sizes))


# --------------------------------------------------------------------------------------
# concentration bound
# --------------------------------------------------------------------------------------

class ConcentrationBoundTest(unittest.TestCase):

    def test_binomial_cdf_endpoints_and_symmetry(self):
        self.assertEqual(cf.binomial_cdf(-1, 10, 0.3), 0.0)
        self.assertEqual(cf.binomial_cdf(10, 10, 0.3), 1.0)
        self.assertAlmostEqual(cf.binomial_cdf(5, 10, 0.5), 0.623046875, places=12)

    def test_binomial_cdf_is_monotone_in_k(self):
        values = [cf.binomial_cdf(k, 40, 0.25) for k in range(41)]
        self.assertEqual(values, sorted(values))

    @unittest.skipUnless(HAVE_SCIPY, "SciPy not installed")
    def test_binomial_cdf_matches_scipy(self):
        for k, n, p in [(0, 5, 0.2), (3, 10, 0.5), (25, 100, 0.2), (450, 1000, 0.45)]:
            self.assertAlmostEqual(cf.binomial_cdf(k, n, p),
                                   float(_scipy_stats.binom.cdf(k, n, p)), places=9)

    def test_no_evidence_when_the_observed_risk_already_exceeds_alpha(self):
        self.assertEqual(cf.hoeffding_bentkus_p(0.10, 500, 0.05), 1.0)
        self.assertEqual(cf.hoeffding_bentkus_p(0.05, 500, 0.05), 1.0)

    def test_evidence_strengthens_with_more_calibration_data(self):
        small = cf.hoeffding_bentkus_p(0.02, 100, 0.05)
        large = cf.hoeffding_bentkus_p(0.02, 2000, 0.05)
        self.assertLess(large, small)
        self.assertLess(large, 0.001)

    def test_evidence_strengthens_as_the_observed_risk_falls(self):
        self.assertLess(cf.hoeffding_bentkus_p(0.01, 400, 0.05),
                        cf.hoeffding_bentkus_p(0.04, 400, 0.05))

    def test_is_never_looser_than_either_bound_alone(self):
        for risk, n, alpha in [(0.01, 200, 0.05), (0.03, 800, 0.05), (0.002, 150, 0.02)]:
            hoeffding = math.exp(-n * cf._kl(risk, alpha))
            bentkus = math.e * cf.binomial_cdf(math.ceil(n * risk), n, alpha)
            self.assertLessEqual(cf.hoeffding_bentkus_p(risk, n, alpha),
                                 min(1.0, hoeffding, bentkus) + 1e-15)


# --------------------------------------------------------------------------------------
# risk-controlling thresholds
# --------------------------------------------------------------------------------------

class RiskControlTest(unittest.TestCase):

    # Most conservative (abstain most) first — the fixed sequence the walk depends on.
    GRID = [round(1.0 - 0.05 * i, 2) for i in range(19)]

    def certify(self, sample, alpha=0.05, delta=0.05):
        return cf.risk_controlling_threshold(
            self.GRID, lambda t: sufficiency_risk(sample, t), alpha, delta,
            abstention_fn=lambda t: sum(1 for s, _ in sample if s < t) / len(sample))

    def sample(self, n, seed):
        rng = random.Random(seed)
        return [make_sufficiency_query(rng) for _ in range(n)]

    def test_certifies_a_threshold_on_a_healthy_calibration_set(self):
        certificate = self.certify(self.sample(1500, 1))
        self.assertTrue(certificate.certified)
        self.assertLess(certificate.empirical_risk, 0.05)
        self.assertLessEqual(certificate.p_value, 0.05)
        self.assertIn("holds the risk at or below", certificate.statement())

    def test_picks_the_most_permissive_threshold_the_evidence_supports(self):
        # The point of the fixed sequence: not merely *a* safe threshold, but the least
        # conservative one, so the guarantee costs as few needless abstentions as possible.
        sample = self.sample(1500, 2)
        certificate = self.certify(sample)
        index = self.GRID.index(certificate.threshold)
        self.assertGreater(index, 0)
        next_risk, n = sufficiency_risk(sample, self.GRID[index + 1])
        self.assertGreater(cf.hoeffding_bentkus_p(next_risk, n, 0.05), 0.05)

    def test_a_tighter_alpha_yields_a_more_conservative_threshold(self):
        sample = self.sample(2000, 3)
        loose = self.certify(sample, alpha=0.10).threshold
        tight = self.certify(sample, alpha=0.02).threshold
        self.assertGreater(tight, loose)

    def test_reports_what_the_guarantee_cost_in_abstentions(self):
        certificate = self.certify(self.sample(1500, 4))
        self.assertGreater(certificate.empirical_abstention, 0.0)
        self.assertLess(certificate.empirical_abstention, 1.0)

    def test_a_useless_score_is_certified_only_by_abstaining_almost_always(self):
        # Every context is insufficient and the score says otherwise. Risk can still be
        # controlled — by refusing to answer — and the certificate makes that visible as a
        # near-total abstention rate rather than hiding it behind a passing threshold.
        rng = random.Random(5)
        hopeless = [(rng.betavariate(6, 2), False) for _ in range(800)]
        certificate = cf.risk_controlling_threshold(
            self.GRID, lambda t: sufficiency_risk(hopeless, t), 0.05, 0.05,
            abstention_fn=lambda t: sum(1 for s, _ in hopeless if s < t) / len(hopeless))
        self.assertTrue(certificate.certified)
        self.assertGreaterEqual(certificate.threshold, 0.85)
        self.assertGreater(certificate.empirical_abstention, 0.85)

    def test_refuses_to_certify_when_even_the_safest_candidate_is_unsafe(self):
        # A grid that cannot abstain enough: the honest output is no threshold at all,
        # not the safest-looking grid point dressed up as a guarantee.
        rng = random.Random(5)
        hopeless = [(rng.betavariate(6, 2), False) for _ in range(800)]
        shallow_grid = [0.5, 0.45, 0.40]
        certificate = cf.risk_controlling_threshold(
            shallow_grid, lambda t: sufficiency_risk(hopeless, t), 0.05, 0.05)
        self.assertFalse(certificate.certified)
        self.assertIsNone(certificate.threshold)
        self.assertIn("abstains on everything", certificate.statement())

    def test_a_tiny_calibration_set_cannot_certify_much(self):
        certificate = self.certify(self.sample(20, 6))
        if certificate.certified:
            self.assertGreaterEqual(certificate.threshold, 0.8)   # forced very conservative

    def test_rejects_impossible_error_budgets(self):
        with self.assertRaises(ValueError):
            cf.risk_controlling_threshold([0.5], lambda t: (0.0, 10), alpha=0.0)
        with self.assertRaises(ValueError):
            cf.risk_controlling_threshold([0.5], lambda t: (0.0, 10), delta=1.0)

    def test_the_guarantee_holds_across_repeated_calibrate_and_deploy(self):
        # The claim, run end to end: P(true risk of the deployed threshold > alpha) <= delta,
        # where the probability is over the calibration draw. Fit on one sample, measure on
        # a fresh one, repeat, and count how often the promise was broken.
        alpha, delta, trials = 0.05, 0.05, 120
        violations = certified = 0
        for trial in range(trials):
            calibration = self.sample(600, 7000 + trial)
            certificate = self.certify(calibration, alpha, delta)
            if not certificate.certified:
                continue
            certified += 1
            deployed_risk, _ = sufficiency_risk(self.sample(3000, 9000 + trial),
                                                certificate.threshold)
            violations += deployed_risk > alpha
        self.assertGreater(certified, trials * 0.8, "expected most trials to certify")
        self.assertLessEqual(violations / certified, delta + 0.03,
                             f"guarantee broken in {violations}/{certified} deployments")


if __name__ == "__main__":
    unittest.main(verbosity=2)
