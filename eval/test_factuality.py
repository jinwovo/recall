"""Tests for claim-level conformal factuality (eval/factuality.py).

Each promise is checked the way test_conformal.py checks its own: calibrate on one sample,
deploy on a fresh one, repeat a few hundred times, and count. The strict promise is exact,
so it is checked from both sides — a threshold that over-covers by a wide margin is
throwing claims away for nothing, and is as much a bug as one that under-covers.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import math
import random
import unittest

import factuality as fa
from factuality import Claim


def make_answer(rng: random.Random, p_false: float = 0.2, informative: bool = True,
                distractor: bool = False) -> list[Claim]:
    """An answer of 2-8 claims. Supported claims tend to score high, false ones low —
    unless the scorer is uninformative, or distractor passages make false claims look
    entailed (the shift arXiv 2603.16817 warns about)."""
    claims = []
    for _ in range(rng.randint(2, 8)):
        ok = rng.random() >= p_false
        if not informative:
            s = rng.random()
        elif ok:
            s = rng.betavariate(5, 2)
        else:
            s = rng.betavariate(4, 3) if distractor else rng.betavariate(2, 5)
        claims.append(Claim(s, ok))
    return claims


def sample(rng, n, **kw):
    return [make_answer(rng, **kw) for _ in range(n)]


class ClosedForms(unittest.TestCase):

    def test_nonconformity_is_the_top_false_score(self):
        a = [Claim(0.9, True), Claim(0.4, False), Claim(0.7, False)]
        self.assertEqual(fa.nonconformity(a), 0.7)
        self.assertEqual(fa.nonconformity([Claim(0.2, True)]), -math.inf)

    def test_a_claim_at_the_threshold_is_dropped(self):
        a = [Claim(0.5, False), Claim(0.6, True)]
        self.assertEqual([c.score for c in fa.retained(a, 0.5)], [0.6])
        # ...which is exactly what makes "nonconformity <= threshold" mean "factual".
        self.assertTrue(all(c.supported for c in fa.retained(a, fa.nonconformity(a))))

    def test_strict_threshold_is_the_corrected_quantile(self):
        answers = [[Claim(s, False)] for s in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
        cert = fa.calibrate_strict(answers, alpha=0.2)
        # rank ceil((9+1) * 0.8) = 8 → the 8th smallest.
        self.assertEqual(cert.threshold, 0.8)

    def test_too_few_answers_refuses_rather_than_guessing(self):
        rng = random.Random(1)
        cert = fa.calibrate_strict(sample(rng, 8), alpha=0.1)   # needs at least 9
        self.assertFalse(cert.certified)
        self.assertIn("at least 9", cert.statement())
        self.assertFalse(fa.calibrate_expected_share(sample(rng, 8), alpha=0.1).certified)

    def test_an_all_true_calibration_keeps_everything_and_is_still_certified(self):
        answers = [[Claim(0.3, True), Claim(0.9, True)] for _ in range(30)]
        cert = fa.calibrate_strict(answers, alpha=0.1)
        self.assertEqual(cert.threshold, -math.inf)
        self.assertTrue(cert.usable)
        self.assertEqual(cert.calibration["retention"], 1.0)

    def test_nan_scores_are_rejected_at_the_boundary(self):
        with self.assertRaises(ValueError):
            Claim(float("nan"), True)

    def test_alpha_is_validated(self):
        for bad in (0.0, 1.0, -0.1):
            with self.assertRaises(ValueError):
                fa.calibrate_strict([[Claim(0.1, True)]], alpha=bad)


class GuaranteesBySimulation(unittest.TestCase):
    RUNS, N_CAL, N_TEST, ALPHA = 300, 200, 400, 0.10

    def _cycle(self, fit, metric, seed=7, **test_kw):
        rng = random.Random(seed)
        realised, retention = [], []
        for _ in range(self.RUNS):
            cert = fit(sample(rng, self.N_CAL), alpha=self.ALPHA)
            ev = fa.evaluate(sample(rng, self.N_TEST, **test_kw), cert.threshold)
            realised.append(ev[metric])
            retention.append(ev["retention"])
        return sum(realised) / len(realised), sum(retention) / len(retention)

    def test_strict_promise_holds_and_is_tight(self):
        factual, retention = self._cycle(fa.calibrate_strict, "all_supported")
        # Mean over runs is the marginal probability; MC error at 300 x 400 is ~0.2pt.
        self.assertGreaterEqual(factual, 1 - self.ALPHA - 0.01)
        self.assertLessEqual(factual, 1 - self.ALPHA + 1 / (self.N_CAL + 1) + 0.01)
        self.assertGreater(retention, 0.3)

    def test_the_finite_sample_corrections_are_what_hold_the_line_at_small_n(self):
        # At n = 200 the (n+1) corrections move the answer by about 1/(n+1) — inside the
        # Monte Carlo error, so a version without them passes the tests above. At n = 19
        # the strict quantile without it covers 85%, and risk control without its +B term
        # runs at ~10.2% against a 10% promise. These two checks are the ones that fail.
        rng = random.Random(21)
        factual, risk = [], []
        for _ in range(2000):
            strict = fa.calibrate_strict(sample(rng, 19), alpha=self.ALPHA)
            factual.append(fa.evaluate(sample(rng, 50), strict.threshold)["all_supported"])
        for _ in range(600):
            share = fa.calibrate_expected_share(sample(rng, 30), alpha=self.ALPHA)
            risk.append(fa.evaluate(sample(rng, 200), share.threshold)["false_kept_share"])
        self.assertGreaterEqual(sum(factual) / len(factual), 1 - self.ALPHA - 0.01)
        self.assertLessEqual(sum(risk) / len(risk), self.ALPHA - 0.01)

    def test_expected_share_promise_holds_and_keeps_more(self):
        risk, retention = self._cycle(fa.calibrate_expected_share, "false_kept_share")
        self.assertLessEqual(risk, self.ALPHA + 0.005)
        _, strict_retention = self._cycle(fa.calibrate_strict, "all_supported")
        # The weaker promise has to buy something, or there is no reason to offer it.
        self.assertGreater(retention, strict_retention + 0.1)

    def test_an_uninformative_scorer_keeps_the_promise_by_emptying_answers(self):
        rng = random.Random(3)
        cert = fa.calibrate_strict(sample(rng, 300, informative=False), alpha=0.1)
        ev = fa.evaluate(sample(rng, 2000, informative=False), cert.threshold)
        self.assertGreaterEqual(ev["all_supported"], 0.88)
        # ...and the certificate says so instead of presenting it as a working filter.
        self.assertTrue(cert.vacuous)
        self.assertIn("VACUOUS", cert.statement())

    def test_distractors_break_the_promise(self):
        # Not a property to preserve — a limitation to keep measured. Calibrated clean,
        # deployed against passages that make false claims look entailed.
        factual, _ = self._cycle(fa.calibrate_strict, "all_supported", distractor=True)
        self.assertLess(factual, 1 - self.ALPHA - 0.05)

    def test_per_group_calibration_holds_where_the_marginal_one_does_not(self):
        rng = random.Random(11)
        marginal_hard, mondrian_hard = [], []
        for _ in range(150):
            easy = sample(rng, 200, p_false=0.05)
            hard = sample(rng, 200, p_false=0.45)
            answers = easy + hard
            groups = ["easy"] * 200 + ["hard"] * 200
            marginal = fa.calibrate_strict(answers, alpha=self.ALPHA)
            certs = fa.calibrate_by_group(answers, groups, alpha=self.ALPHA)
            test_hard = sample(rng, 300, p_false=0.45)
            marginal_hard.append(fa.evaluate(test_hard, marginal.threshold)["all_supported"])
            mondrian_hard.append(fa.evaluate(test_hard, certs["hard"].threshold)["all_supported"])
        self.assertLess(sum(marginal_hard) / 150, 1 - self.ALPHA - 0.02)
        self.assertGreaterEqual(sum(mondrian_hard) / 150, 1 - self.ALPHA - 0.01)

    def test_a_group_too_small_is_refused_not_borrowed(self):
        rng = random.Random(5)
        answers = sample(rng, 105)
        certs = fa.calibrate_by_group(answers, ["big"] * 100 + ["tiny"] * 5, alpha=0.1)
        self.assertTrue(certs["big"].certified)
        self.assertFalse(certs["tiny"].certified)


if __name__ == "__main__":
    unittest.main()
