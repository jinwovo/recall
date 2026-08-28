"""Tests for prediction-powered inference (eval/ppi.py).

The claim is that an interval built from a few hand labels and many *biased* model
predictions still covers the truth. That is either true or the whole thing is a way of
laundering a judge's opinion into a confidence interval, so the central tests simulate the
whole cycle — draw a population, label a little of it, predict all of it badly, build the
interval, and count how often the truth is outside.

Three properties get checked, and they are the three that matter:

1. **Validity under bias.** A judge that is optimistic by eight points must not move the
   coverage rate. If it does, the estimator is worthless.
2. **Safety.** A judge predicting pure noise must cost nothing — lambda goes to zero and
   the estimator becomes the hand-label mean.
3. **Power.** An informative judge must actually narrow the interval, or there is no
   reason to prefer this over labelling by hand.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import random
import unittest

import ppi


def population(rng: random.Random, size: int, bias: float, noise: float,
               signal: float = 1.0) -> list[tuple[float, float]]:
    """(true label, judge prediction) pairs.

    The truth is a graded score in [0, 1]. The judge sees it through a `signal` weight, adds
    a constant `bias`, and blurs it by `noise` — a model that is systematically flattering
    and individually unreliable, which is what an LLM judge grading its own system is.
    """
    pairs = []
    for _ in range(size):
        truth = rng.betavariate(5, 2)
        prediction = signal * truth + bias + rng.gauss(0.0, noise)
        pairs.append((truth, min(1.0, max(0.0, prediction))))
    return pairs


def split(pairs, n_labeled):
    labeled, unlabeled = pairs[:n_labeled], pairs[n_labeled:]
    return ([y for y, _ in labeled], [f for _, f in labeled], [f for _, f in unlabeled])


class CoverageTest(unittest.TestCase):
    """The guarantee, simulated end to end."""

    TRIALS = 400
    N_LABELED = 60
    N_UNLABELED = 1200

    def run_trials(self, bias: float, noise: float, signal: float = 1.0, seed: int = 7):
        """Returns (ppi coverage, labels-only coverage, judge-only coverage, mean widths)."""
        covered = covered_labels = covered_judge = 0
        widths, label_widths = [], []
        # The estimand is the population mean of Beta(5, 2), which is 5/7 exactly — no
        # need to estimate it, and certainly not once per trial.
        truth = 5 / 7
        for trial in range(self.TRIALS):
            rng = random.Random(seed * 1000 + trial)
            pairs = population(rng, self.N_LABELED + self.N_UNLABELED, bias, noise, signal)
            labels, labeled_pred, unlabeled_pred = split(pairs, self.N_LABELED)

            estimate = ppi.ppi_mean(labels, labeled_pred, unlabeled_pred)
            covered += estimate.lo <= truth <= estimate.hi
            widths.append(estimate.width)

            lo, hi = estimate.labeled_only_ci
            covered_labels += lo <= truth <= hi
            label_widths.append(hi - lo)

            # What a report that quotes the judge would claim, with the interval it would
            # attach: many predictions, so a very narrow interval around the wrong number.
            judge = labeled_pred + unlabeled_pred
            jlo, jhi = ppi.classical_interval(judge)
            covered_judge += jlo <= truth <= jhi
        return (covered / self.TRIALS, covered_labels / self.TRIALS,
                covered_judge / self.TRIALS,
                sum(widths) / len(widths), sum(label_widths) / len(label_widths))

    def test_holds_when_the_judge_is_optimistic(self):
        ppi_cov, label_cov, _, _, _ = self.run_trials(bias=0.08, noise=0.10)
        self.assertGreaterEqual(ppi_cov, 0.90, f"PPI covered only {ppi_cov:.1%}")
        self.assertGreaterEqual(label_cov, 0.90)

    def test_holds_when_the_judge_is_pessimistic(self):
        ppi_cov, _, _, _, _ = self.run_trials(bias=-0.12, noise=0.15, seed=11)
        self.assertGreaterEqual(ppi_cov, 0.90, f"PPI covered only {ppi_cov:.1%}")

    def test_quoting_the_judge_directly_is_the_failure_this_replaces(self):
        # The point of the exercise: averaging thousands of biased predictions gives a
        # tight interval around a number that is simply wrong, and it essentially never
        # contains the truth. Nothing in a normal pipeline flags this.
        _, _, judge_cov, _, _ = self.run_trials(bias=0.08, noise=0.10)
        self.assertLess(judge_cov, 0.10, f"expected the naive judge interval to miss")

    def test_an_informative_judge_narrows_the_interval(self):
        _, _, _, ppi_width, label_width = self.run_trials(bias=0.08, noise=0.05)
        self.assertLess(ppi_width, label_width)
        self.assertLess(ppi_width, 0.8 * label_width)

    def test_a_useless_judge_costs_nothing(self):
        # signal=0 makes the prediction pure noise. lambda should collapse and the
        # estimator should land on the hand-label mean, no worse than ignoring the model.
        ppi_cov, label_cov, _, ppi_width, label_width = self.run_trials(
            bias=0.0, noise=0.30, signal=0.0, seed=23)
        self.assertGreaterEqual(ppi_cov, 0.90)
        self.assertLessEqual(ppi_width, label_width * 1.05)


class LambdaTest(unittest.TestCase):

    def test_a_noise_predictor_gets_weight_zero(self):
        rng = random.Random(3)
        labels = [rng.betavariate(5, 2) for _ in range(200)]
        noise = [rng.random() for _ in range(200)]
        self.assertLess(ppi.optimal_lambda(labels, noise, 2000), 0.2)

    def test_a_perfect_predictor_gets_weight_near_one(self):
        rng = random.Random(4)
        labels = [rng.betavariate(5, 2) for _ in range(200)]
        perfect = [y + 0.1 for y in labels]          # biased but perfectly correlated
        self.assertGreater(ppi.optimal_lambda(labels, perfect, 20000), 0.9)

    def test_a_constant_predictor_explains_nothing(self):
        self.assertEqual(ppi.optimal_lambda([0.1, 0.9, 0.5], [0.5, 0.5, 0.5], 100), 0.0)

    def test_weight_stays_inside_the_unit_interval(self):
        rng = random.Random(5)
        for _ in range(50):
            labels = [rng.betavariate(2, 2) for _ in range(30)]
            preds = [rng.gauss(y, 0.3) for y in labels]
            self.assertGreaterEqual(ppi.optimal_lambda(labels, preds, 300), 0.0)
            self.assertLessEqual(ppi.optimal_lambda(labels, preds, 300), 1.0)

    def test_more_unlabelled_data_raises_the_weight(self):
        # With N barely above n the unlabelled term is noisy and the tuning backs off; as N
        # grows the model's contribution is nearly free.
        rng = random.Random(6)
        labels = [rng.betavariate(5, 2) for _ in range(100)]
        preds = [0.9 * y + 0.05 for y in labels]
        self.assertLess(ppi.optimal_lambda(labels, preds, 100),
                        ppi.optimal_lambda(labels, preds, 100_000))


class EstimatorTest(unittest.TestCase):

    def make(self, n=50, N=500, bias=0.1, seed=9, **kwargs):
        rng = random.Random(seed)
        pairs = population(rng, n + N, bias, 0.05)
        labels, labeled_pred, unlabeled_pred = split(pairs, n)
        return ppi.ppi_mean(labels, labeled_pred, unlabeled_pred, **kwargs)

    def test_reports_the_judge_bias_it_corrected_for(self):
        estimate = self.make(bias=0.10)
        self.assertGreater(estimate.judge_bias, 0.0)
        self.assertTrue(estimate.judge_is_optimistic)
        self.assertLess(estimate.estimate, estimate.judge_only)

    def test_lambda_zero_reduces_to_the_hand_label_mean(self):
        estimate = self.make(lam=0.0)
        self.assertAlmostEqual(estimate.estimate, estimate.labeled_only, places=12)
        self.assertAlmostEqual(estimate.width, estimate.labeled_only_width, places=6)

    def test_lambda_one_is_the_original_untuned_estimator(self):
        # Untuned PPI: the full judge-bias correction, no variance-optimal shrinkage.
        estimate = self.make(lam=1.0)
        self.assertEqual(estimate.lam, 1.0)
        self.assertNotEqual(estimate.estimate, estimate.labeled_only)
        tuned = self.make()
        self.assertLessEqual(tuned.width, estimate.width * 1.02)

    def test_effective_sample_size_exceeds_the_labels_actually_written(self):
        estimate = self.make(n=50, N=5000)
        self.assertGreater(estimate.effective_n, estimate.n_labeled)
        self.assertTrue(estimate.narrower_than_labels_alone)

    def test_the_interval_brackets_its_own_estimate(self):
        estimate = self.make()
        self.assertLess(estimate.lo, estimate.estimate)
        self.assertGreater(estimate.hi, estimate.estimate)

    def test_a_tighter_alpha_widens_the_interval(self):
        self.assertGreater(self.make(alpha=0.01).width, self.make(alpha=0.10).width)

    def test_the_statement_names_the_direction_of_the_bias(self):
        self.assertIn("optimistic", self.make(bias=0.10).statement())
        self.assertIn("pessimistic", self.make(bias=-0.10).statement())

    def test_serialises(self):
        payload = self.make().as_dict()
        for key in ("estimate", "lo", "hi", "lambda", "judge_bias", "effective_n"):
            self.assertIn(key, payload)


class InputValidationTest(unittest.TestCase):

    def test_misaligned_labels_and_predictions_are_refused(self):
        with self.assertRaises(ValueError):
            ppi.ppi_mean([0.1, 0.2], [0.1], [0.3, 0.4])

    def test_too_few_hand_labels_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            ppi.ppi_mean([0.5], [0.6], [0.7, 0.8])

    def test_no_unlabelled_predictions_points_at_the_classical_interval(self):
        with self.assertRaises(ValueError):
            ppi.ppi_mean([0.5, 0.6], [0.5, 0.6], [])

    def test_classical_interval_degrades_gracefully_on_a_tiny_sample(self):
        self.assertEqual(ppi.classical_interval([0.5]), (0.0, 1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class QaHarnessIntegrationTest(unittest.TestCase):
    """The path that turns this repo's own groundedness number into a measurement."""

    LABELS = ('{"query": "a", "groundedness": 1.0}\n'
              '{"query": "b", "groundedness": 0.5, "note": "invented a default"}\n'
              '\n'
              '{"query": "c", "groundedness": 0.0}\n')

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text: str) -> str:
        import os
        path = os.path.join(self.tmp.name, "labels.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def harness(self):
        import run_qa_eval
        return run_qa_eval

    def test_reads_labels_and_ignores_blank_lines_and_notes(self):
        labels = self.harness().load_human_labels(self.write(self.LABELS))
        self.assertEqual(labels, {"a": 1.0, "b": 0.5, "c": 0.0})

    def test_a_missing_score_is_refused_with_the_line_number(self):
        with self.assertRaises(SystemExit) as caught:
            self.harness().load_human_labels(self.write('{"query": "a"}\n'))
        self.assertIn(":1:", str(caught.exception))

    def test_a_score_outside_the_scale_is_refused(self):
        with self.assertRaises(SystemExit):
            self.harness().load_human_labels(
                self.write('{"query": "a", "groundedness": 1.5}\n'))

    def test_the_report_measures_and_subtracts_an_optimistic_judge(self):
        import io
        from contextlib import redirect_stdout
        harness = self.harness()
        score = {"SUPPORTED": 1.0, "PARTIAL": 0.5, "UNSUPPORTED": 0.0}
        # The judge calls everything SUPPORTED; the human disagrees on two of three.
        labels = {"a": 1.0, "b": 0.5, "c": 0.0}
        judged = [{"query": q, "verdict": "SUPPORTED"} for q in labels]
        judged += [{"query": f"u{i}", "verdict": "SUPPORTED"} for i in range(30)]
        out = io.StringIO()
        with redirect_stdout(out):
            harness.report_ppi(judged, score, labels)
        text = out.getvalue()
        self.assertIn("judge alone", text)
        self.assertIn("optimistic", text)
        self.assertIn("effective labels", text)

    def test_too_few_labels_refuses_instead_of_producing_a_number(self):
        import io
        from contextlib import redirect_stdout
        harness = self.harness()
        score = {"SUPPORTED": 1.0}
        out = io.StringIO()
        with redirect_stdout(out):
            harness.report_ppi([{"query": "a", "verdict": "SUPPORTED"}], score, {"a": 1.0})
        self.assertIn("needs at least", out.getvalue())
        self.assertNotIn("PPI  ", out.getvalue())
