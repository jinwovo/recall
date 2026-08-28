"""Tests for adaptive conformal inference (eval/adaptive.py).

The claim is unusual and worth stating precisely: realised long-run miscoverage converges
to the target **under any distribution shift whatsoever**, including one chosen to defeat
the controller. That is a deterministic statement, not a probabilistic one, so the central
test checks it as an identity rather than by averaging over trials.

Around it sit the tests that give the identity meaning:

* the negative control — a fixed conformal threshold, calibrated once and then left alone,
  losing coverage as soon as the data moves. Without it, "the adaptive one held" says
  nothing about whether anything was at stake.
* recovery timing, since a controller that eventually converges but takes ten thousand
  queries to notice a shift is not useful in a system that reindexes nightly.
* DtACI matching a well-chosen gamma without being told which one it is.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import math
import random
import unittest

import adaptive
from conformal import conformal_quantile


def stream(rng: random.Random, n: int, mean: float, spread: float = 0.15) -> list[float]:
    """Nonconformity scores from a Beta-ish population centred on `mean`."""
    scores = []
    for _ in range(n):
        value = rng.gauss(mean, spread)
        scores.append(min(1.0, max(0.0, value)))
    return scores


def fixed_threshold_coverage(calibration: list[float], live: list[float],
                             alpha: float) -> float:
    """The ADR-0013 arrangement: calibrate once, apply forever."""
    threshold = conformal_quantile(calibration, alpha)
    covered = sum(1 for s in live if s <= threshold)
    return covered / len(live)


class DeterministicBoundTest(unittest.TestCase):
    """The guarantee, checked as the identity it is."""

    def test_the_telescoped_identity_holds_exactly(self):
        # Summing alpha_{t+1} = alpha_t + gamma(alpha - err_t) gives
        #   (1/T) sum(err_t) - alpha = (alpha_1 - alpha_{T+1}) / (T * gamma)
        # with no approximation anywhere. If this drifts, the update is wrong.
        rng = random.Random(3)
        controller = adaptive.AdaptiveConformal(alpha=0.10, gamma=0.05, window=200)
        alpha_1 = controller.alpha_t
        for score in stream(rng, 800, 0.5):
            controller.observe(score)
        left = controller.realized_miscoverage - controller.alpha
        right = (alpha_1 - controller.alpha_t) / (controller.steps * controller.gamma)
        self.assertAlmostEqual(left, right, places=10)

    def test_the_level_never_escapes_its_interval(self):
        # An empty set always misses and a full set never does, so alpha_t is trapped in
        # [-gamma, 1 + gamma]. That containment is a property of the *feedback*: coverage
        # has to be read off the set the level produced. Forcing an outcome that the set
        # could not have produced — claiming a miss while the set holds everything —
        # breaks the premise, and alpha_t then runs away exactly as the algebra says it
        # should. So these adversaries move the scores, never the verdict.
        adversaries = {
            "always just outside": lambda c, _: min(1.0, c.threshold() + 1e-6)
                                   if math.isfinite(c.threshold()) else 1.0,
            "always inside": lambda c, _: 0.0,
            "alternating": lambda c, step: (0.0 if step % 2 else
                                            (min(1.0, c.threshold() + 1e-6)
                                             if math.isfinite(c.threshold()) else 1.0)),
        }
        for name, choose in adversaries.items():
            controller = adaptive.AdaptiveConformal(alpha=0.10, gamma=0.1, window=100)
            controller.scores.extend(i / 100 for i in range(100))
            for step in range(500):
                controller.observe(choose(controller, step))
                self.assertGreaterEqual(controller.alpha_t, -controller.gamma - 1e-9, name)
                self.assertLessEqual(controller.alpha_t, 1 + controller.gamma + 1e-9, name)

    def test_the_reported_bound_is_respected_under_adversarial_input(self):
        # The strongest adversary available: every score placed a hair outside whatever the
        # controller currently accepts, so it misses on purpose for as long as it can. It
        # cannot do so forever — once the level drops below zero the set holds everything —
        # and that is precisely why the bound is not merely probabilistic.
        controller = adaptive.AdaptiveConformal(alpha=0.10, gamma=0.05, window=200)
        controller.scores.extend(i / 200 for i in range(200))
        for _ in range(2000):
            cutoff = controller.threshold()
            controller.observe(min(1.0, cutoff + 1e-6) if math.isfinite(cutoff) else 1.0)
        self.assertTrue(controller.within_bound, controller.report())

    def test_the_bound_shrinks_as_the_stream_grows(self):
        early = adaptive.coverage_bound(0.1, 0.05, 100)
        late = adaptive.coverage_bound(0.1, 0.05, 10_000)
        self.assertLess(late, early)
        self.assertLess(late, 0.05)


class DriftTest(unittest.TestCase):
    """What the fixed threshold of ADR 0013 does when the corpus moves, and what this does."""

    ALPHA = 0.10

    def scenario(self, shift: float, gradual: bool, n: int = 3000, seed: int = 11):
        """Calibrate on one distribution, then serve a shifted one."""
        rng = random.Random(seed)
        calibration = stream(rng, 600, 0.50)
        live = []
        for step in range(n):
            fraction = step / n if gradual else (0.0 if step < n // 3 else 1.0)
            live.append(min(1.0, max(0.0, rng.gauss(0.50 + shift * fraction, 0.15))))
        return calibration, live

    def run_adaptive(self, calibration, live, gamma=0.05):
        controller = adaptive.AdaptiveConformal(self.ALPHA, gamma, window=600)
        for score in calibration:
            controller.scores.append(score)          # seed the window, no feedback yet
        for score in live:
            controller.observe(score)
        return controller

    def test_a_fixed_threshold_loses_coverage_when_the_scores_shift(self):
        # The negative control. Nothing about this is exotic: the reranker starts scoring
        # differently and a threshold frozen in January stops meaning 90%.
        calibration, live = self.scenario(shift=0.30, gradual=False)
        fixed = fixed_threshold_coverage(calibration, live, self.ALPHA)
        self.assertLess(fixed, 0.75, f"expected the fixed threshold to fail, got {fixed:.3f}")

    def test_the_controller_holds_through_the_same_shift(self):
        calibration, live = self.scenario(shift=0.30, gradual=False)
        controller = self.run_adaptive(calibration, live)
        self.assertGreater(1 - controller.realized_miscoverage, 0.85)
        self.assertTrue(controller.within_bound)

    def test_it_holds_through_a_gradual_drift_too(self):
        calibration, live = self.scenario(shift=0.30, gradual=True, seed=17)
        controller = self.run_adaptive(calibration, live)
        self.assertGreater(1 - controller.realized_miscoverage, 0.85)

    def test_it_recovers_within_a_few_over_gamma_steps(self):
        # A controller that converges eventually but takes ten thousand queries to notice
        # is no use to a system that reindexes nightly. Recovery should take on the order
        # of 1/gamma steps, not the length of the stream.
        calibration, live = self.scenario(shift=0.35, gradual=False, n=3000, seed=23)
        controller = adaptive.AdaptiveConformal(self.ALPHA, 0.05, window=600)
        for score in calibration:
            controller.scores.append(score)
        boundary = 1000                                   # where the shift lands
        recent_misses = []
        for index, score in enumerate(live):
            covered = controller.observe(score)
            if index >= boundary + 400:                   # after ~8/gamma steps
                recent_misses.append(0 if covered else 1)
        settled = sum(recent_misses) / len(recent_misses)
        self.assertLess(abs(settled - self.ALPHA), 0.06,
                        f"still off target after recovery: {settled:.3f}")

    def test_a_location_shift_is_absorbed_by_the_window_not_by_the_level(self):
        # The result that corrected this module's original claim, so it is checked rather
        # than described. A pure location shift never reaches the level controller at all:
        # the rolling window refills with post-shift scores, the quantile moves with them,
        # and alpha_t lands in the same place it would have with no shift whatsoever. The
        # controller is not failing to notice — there is nothing left to notice.
        #
        # Sampling the terminal level over 40 seeds at each of three shift sizes, the
        # widest excursion from target is 0.100 — two gamma — and it is the *same* 0.100
        # whether the shift is zero, 0.20 or 0.30. The level is not being pushed anywhere.
        # (At 0.40 it does begin to bite, because a mean of 0.90 against a 0.15 spread puts
        # a quarter of the scores against the clamp at 1.0, and that is the saturation case
        # tested below rather than a location shift.) This is why the negative control
        # above matters: the fixed threshold loses coverage on this very data.
        excursions = {}
        for shift in (0.0, 0.20, 0.30):
            levels = [self.run_adaptive(*self.scenario(shift=shift, gradual=False,
                                                       seed=seed)).alpha_t
                      for seed in range(40)]
            excursions[shift] = max(abs(level - self.ALPHA) for level in levels)
        for shift, widest in excursions.items():
            self.assertAlmostEqual(widest, excursions[0.0], places=9, msg=f"shift {shift}")
            self.assertLess(widest, 4 * 0.05, msg=f"shift {shift}")

    def test_compensation_is_reported_only_when_the_window_cannot_track(self):
        # `compensating` is deliberately not a drift detector, and the first two cases are
        # what force the distinction: stationary data and a clean location shift both leave
        # it False, because in both the window is doing its job and the level has no
        # standing offset to hold.
        rng = random.Random(31)
        calm = adaptive.AdaptiveConformal(self.ALPHA, 0.05, window=400)
        for score in stream(rng, 400, 0.5):
            calm.scores.append(score)
        for score in stream(rng, 1500, 0.5):
            calm.observe(score)
        self.assertFalse(calm.compensating, calm.report())

        shifted = self.run_adaptive(*self.scenario(shift=0.30, gradual=False, seed=37))
        self.assertFalse(shifted.compensating, shifted.report())

        # What does fire is a score distribution the quantile cannot resolve. `stream`
        # clamps at 1.0, so centring it at 1.05 piles ~62% of the mass onto a single tied
        # value — the saturating reranker, which is a real pathology and not a synthetic
        # one: once most items share the top score no threshold separates them, coverage
        # overshoots whatever is asked for, and only a badly displaced level keeps the
        # realised rate near target. Terminal alpha_t here is 0.55-0.65 against a 0.30
        # boundary, so the signal is not marginal.
        saturated = adaptive.AdaptiveConformal(self.ALPHA, 0.05, window=400)
        rng = random.Random(37)
        for score in stream(rng, 400, 0.5):
            saturated.scores.append(score)
        for score in list(stream(rng, 800, 0.5)) + stream(rng, 1200, 1.05):
            saturated.observe(score)
        self.assertTrue(saturated.compensating, saturated.report())
        self.assertLess(abs(saturated.realized_miscoverage - self.ALPHA), 0.02,
                        "the level should still be holding the target rate")


class DtACITest(unittest.TestCase):
    """Removing gamma, which is the last hand-picked constant in the loop."""

    ALPHA = 0.10

    def stream_with_shift(self, seed=41, n=3000):
        rng = random.Random(seed)
        calibration = stream(rng, 600, 0.50)
        live = [min(1.0, max(0.0, rng.gauss(0.50 + (0.0 if i < n // 3 else 0.30), 0.15)))
                for i in range(n)]
        return calibration, live

    def drive(self, controller, calibration, live):
        for score in calibration:
            controller.scores.append(score)
            if hasattr(controller, "experts"):
                for expert in controller.experts:
                    expert.scores.append(score)
        for score in live:
            controller.observe(score)
        return controller

    def test_it_tracks_the_target_without_being_told_the_step_size(self):
        calibration, live = self.stream_with_shift()
        aggregated = self.drive(adaptive.DtACI(self.ALPHA), calibration, live)
        self.assertLess(abs(aggregated.realized_miscoverage - self.ALPHA), 0.05,
                        aggregated.report())

    def test_it_is_competitive_with_the_best_fixed_gamma_chosen_in_hindsight(self):
        calibration, live = self.stream_with_shift(seed=43)
        aggregated = self.drive(adaptive.DtACI(self.ALPHA), calibration, live)
        best = min(
            abs(self.drive(adaptive.AdaptiveConformal(self.ALPHA, g, 600),
                           calibration, live).realized_miscoverage - self.ALPHA)
            for g in adaptive.GAMMA_CANDIDATES)
        gap = abs(aggregated.realized_miscoverage - self.ALPHA)
        self.assertLess(gap, best + 0.04,
                        f"aggregate off by {gap:.3f}, best hindsight gamma off by {best:.3f}")

    def test_weights_are_a_distribution_and_favour_one_candidate(self):
        calibration, live = self.stream_with_shift(seed=47)
        aggregated = self.drive(adaptive.DtACI(self.ALPHA), calibration, live)
        weights = aggregated.weights
        self.assertAlmostEqual(sum(weights), 1.0, places=9)
        self.assertTrue(all(w >= 0 for w in weights))
        self.assertIn(aggregated.best_gamma, adaptive.GAMMA_CANDIDATES)
        self.assertGreater(max(weights), 1.5 / len(weights))

    def test_it_refuses_an_empty_candidate_set(self):
        with self.assertRaises(ValueError):
            adaptive.DtACI(self.ALPHA, gammas=())


class InterfaceTest(unittest.TestCase):

    def test_an_empty_window_keeps_everything_rather_than_nothing(self):
        # Before any calibration data arrives the honest set is the whole candidate list;
        # returning an empty one would silently drop every answer.
        controller = adaptive.AdaptiveConformal()
        self.assertEqual(controller.threshold(), math.inf)
        self.assertTrue(controller.covers(0.99))

    def test_the_extremes_of_the_level_behave_as_the_recursion_needs(self):
        controller = adaptive.AdaptiveConformal(alpha=0.10, gamma=0.05)
        controller.scores.extend([0.1, 0.2, 0.3, 0.4, 0.5])
        controller.alpha_t = -0.01
        self.assertEqual(controller.threshold(), math.inf)      # cannot miss
        self.assertTrue(controller.covers(1.0))
        controller.alpha_t = 1.01
        self.assertEqual(controller.threshold(), -math.inf)     # always misses
        self.assertFalse(controller.covers(0.0))

    def test_the_window_bounds_what_the_calibration_set_remembers(self):
        controller = adaptive.AdaptiveConformal(window=50)
        for value in range(200):
            controller.observe(value / 200)
        self.assertEqual(len(controller.scores), 50)
        self.assertEqual(controller.steps, 200)

    def test_an_explicit_outcome_overrides_the_score_comparison(self):
        # Coverage is not always readable from the score — the serving loop learns it from
        # a judge verdict — so the caller has to be able to say. A score of 0.0 is well
        # inside any set, yet the reported miss is what drives the update.
        controller = adaptive.AdaptiveConformal(alpha=0.10, gamma=0.05)
        controller.scores.extend([0.5] * 50)
        before = controller.alpha_t
        controller.observe(0.0, covered=False)
        # A miss *lowers* the level, because a lower level is a wider set: the quantile is
        # taken at rank ceil((n+1)(1-alpha)), so less alpha means more of the ranking kept.
        self.assertLess(controller.alpha_t, before)
        self.assertEqual(controller.misses, 1)

    def test_invalid_settings_are_refused(self):
        for bad_alpha in (0.0, 1.0, -0.1):
            with self.assertRaises(ValueError):
                adaptive.AdaptiveConformal(alpha=bad_alpha)
        with self.assertRaises(ValueError):
            adaptive.AdaptiveConformal(gamma=0.0)

    def test_the_report_carries_what_an_operator_would_ask(self):
        controller = adaptive.AdaptiveConformal()
        for value in range(300):
            controller.observe((value % 100) / 100)
        report = controller.report()
        for key in ("alpha_target", "alpha_now", "realized_miscoverage", "bound",
                    "within_bound", "compensating", "calibration_points"):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
