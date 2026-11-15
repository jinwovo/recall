"""Tests for active, anytime-valid certification (eval/active.py, docs/adr/0018).

Validity is checked the only way it can be: repeat the whole label-and-estimate cycle many
times against a known truth and count the misses. Each judge below is deliberately wrong in
a different way, because "valid whatever the judge does" is the claim under test.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

import active as ac
import factuality as fa
from conformal import conformal_quantile
from test_factuality import make_answer

THETA = 0.65            # the true groundedness rate the simulated traffic has

# P(judge prediction | true label). Three verdicts, as serving's judge emits.
JUDGES = {
    # Right most of the time, unsure when it should be.
    "calibrated": {1: [(1.0, .80), (.5, .17), (0.0, .03)],
                   0: [(0.0, .60), (.5, .32), (1.0, .08)]},
    # Says SUPPORTED to a third of the unsupported answers: the common failure.
    "flattering": {1: [(1.0, .90), (.5, .10)],
                   0: [(1.0, .35), (.5, .40), (0.0, .25)]},
    # Unsure on the easy answers, certain on the ones it gets wrong. The worst case for
    # an uncertainty-driven design.
    "confidently wrong": {1: [(.5, .60), (1.0, .40)],
                          0: [(1.0, .50), (0.0, .50)]},
}


def _pick(rng, table):
    r, acc = rng.random(), 0.0
    for value, p in table:
        acc += p
        if r < acc:
            return value
    return table[-1][0]


def traffic(rng, n, judge, theta=THETA):
    """(true label, judge prediction) for n answers."""
    out = []
    for _ in range(n):
        y = int(rng.random() < theta)
        out.append((y, _pick(rng, JUDGES[judge][y])))
    return out


def claim_traffic(rng, n, judge):
    """Answers made of claims, with the judge's verdict on each answer. Hard answers carry
    more false claims, so whatever the judge finds uncertain is also where the claim
    nonconformity lives: the selection is informative about the score being calibrated."""
    out = []
    for _ in range(n):
        claims = make_answer(rng, p_false=0.30 if rng.random() < 0.4 else 0.03)
        y = int(all(c.supported for c in claims))
        out.append((claims, y, _pick(rng, JUDGES[judge][y])))
    return out


def label(rng, items, pis):
    """Toss each item's coin; only the selected ones get their label read."""
    selected = [rng.random() < p for p in pis]
    labels = [float(y) if s else None for (y, *_), s in zip(items, selected)]
    return selected, labels


def two_periods(rng, judge, n, budget, design, floor=0.01):
    """Period 1 labels uniformly (the cold start); period 2 uses `design`. Returns period 2."""
    first = traffic(rng, n, judge)
    pi1 = [budget] * n
    s1, l1 = label(rng, first, pi1)
    second = traffic(rng, n, judge)
    pred = [f for _, f in second]
    if design == "uniform":
        pi2 = [budget] * n
    elif design == "textbook":
        pi2 = ac.inclusion_probabilities([ac.judge_uncertainty(f) for f in pred], budget,
                                         floor, mix=0.0)
    else:
        d = ac.design_by_verdict([f for _, f in first], pi1, s1, l1, budget, floor)
        pi2 = [d["pi"][ac.ResidualModel.level(f)] for f in pred]
    s2, l2 = label(rng, second, pi2)
    return pred, pi2, s2, l2


# --------------------------------------------------------------------------------------

class Design(unittest.TestCase):

    def test_the_textbook_uncertainty_trusts_confident_verdicts_completely(self):
        self.assertEqual(ac.judge_uncertainty(1.0), 0.0)
        self.assertEqual(ac.judge_uncertainty(0.0), 0.0)
        self.assertAlmostEqual(ac.judge_uncertainty(0.5), 0.5)

    def test_probabilities_spend_the_budget_and_respect_the_floor(self):
        rng = random.Random(3)
        u = [rng.random() ** 3 for _ in range(5000)]
        for mix in (0.0, 0.5, 1.0):
            pis = ac.inclusion_probabilities(u, 0.1, floor=0.02, mix=mix)
            self.assertGreaterEqual(min(pis), 0.02)
            self.assertLessEqual(max(pis), 1.0)
            # The floor can only add spend, and at most `floor` of it.
            self.assertGreaterEqual(sum(pis) / len(pis), 0.1 - 1e-9)
            self.assertLessEqual(sum(pis) / len(pis), 0.12 + 1e-9)
        self.assertEqual(set(ac.inclusion_probabilities(u, 0.1, mix=1.0)), {0.1})

    def test_more_uncertain_items_are_labelled_more_often(self):
        pis = ac.inclusion_probabilities([0.1, 0.2, 0.4, 0.8], 0.2, floor=0.01, mix=0.0)
        self.assertEqual(pis, sorted(pis))
        self.assertLess(pis[0], pis[-1])

    def test_weights_spend_the_budget_against_traffic_not_against_levels(self):
        # Two verdicts, one 9x as common: the budget is a share of *answers*.
        pis = ac.inclusion_probabilities([0.2, 0.4], 0.1, floor=0.01, mix=0.0, weights=[9, 1])
        self.assertAlmostEqual((9 * pis[0] + pis[1]) / 10, 0.1, places=6)

    def test_bad_design_parameters_are_refused(self):
        with self.assertRaises(ValueError):
            ac.inclusion_probabilities([0.5], 0.0)
        with self.assertRaises(ValueError):
            ac.inclusion_probabilities([0.5], 0.1, floor=0.2)
        with self.assertRaises(ValueError):
            ac.inclusion_probabilities([0.5], 0.1, mix=1.5)

    def test_with_no_labels_the_design_is_uniform(self):
        pred = [1.0] * 70 + [0.5] * 20 + [0.0] * 10
        d = ac.design_by_verdict(pred, [0.1] * 100, [False] * 100, [None] * 100, 0.1)
        self.assertEqual(d["mix"], 1.0)
        self.assertEqual(set(round(p, 9) for p in d["pi"].values()), {0.1})

    def test_the_residual_model_learns_where_the_judge_errs(self):
        # SUPPORTED is right 95% of the time, PARTIAL is a coin.
        pred = [1.0] * 200 + [0.5] * 200
        labels = [1.0] * 190 + [0.0] * 10 + [1.0, 0.0] * 100
        model = ac.ResidualModel().fit(pred, labels)
        self.assertLess(model.uncertainty(1.0), model.uncertainty(0.5))
        self.assertAlmostEqual(model.mse(0.5), 0.25, places=3)
        # An unseen verdict falls back to the prior rather than to "never wrong".
        self.assertEqual(model.mse(0.0), 0.25)

    def test_the_mix_falls_back_to_uniform_when_uncertainty_says_nothing(self):
        rng = random.Random(5)
        pred = [rng.choice([0.0, 0.5, 1.0]) for _ in range(400)]
        labels = [float(rng.random() < 0.5) for _ in pred]
        self.assertEqual(ac.choose_mix(pred, labels, 0.1, uncertainty=lambda f: 0.3), 1.0)


class FixedSample(unittest.TestCase):

    def test_labelling_everything_gives_the_label_mean_exactly(self):
        pred = [1.0, 0.5, 0.0, 1.0]
        labels = [1.0, 0.0, 0.0, 1.0]
        e = ac.active_mean(pred, [1.0] * 4, [True] * 4, labels)
        self.assertAlmostEqual(e.estimate, 0.5)

    def test_a_selected_item_without_a_label_is_an_error_not_a_skip(self):
        with self.assertRaises(ValueError):
            ac.active_mean([1.0, 0.0], [0.5, 0.5], [True, False], [None, None])

    def test_unbiased_whatever_the_judge(self):
        for judge in JUDGES:
            estimates = []
            for r in range(200):
                rng = random.Random(f"bias:{judge}:{r}")
                pred, pi, s, l = two_periods(rng, judge, 1500, 0.1, "learned")
                estimates.append(ac.active_mean(pred, pi, s, l).estimate)
            self.assertAlmostEqual(sum(estimates) / len(estimates), THETA, delta=0.01, msg=judge)

    def test_the_learned_design_covers_and_is_no_wider_than_uniform(self):
        for judge in JUDGES:
            cover, width = {}, {}
            for design in ("uniform", "learned"):
                hits, widths = 0, []
                for r in range(200):
                    rng = random.Random(f"cov:{judge}:{design}:{r}")
                    e = ac.active_mean(*two_periods(rng, judge, 2000, 0.05, design))
                    hits += e.lo <= THETA <= e.hi
                    widths.append(e.width)
                cover[design], width[design] = hits / 200, sum(widths) / 200
            self.assertGreaterEqual(cover["learned"], 0.91, judge)
            self.assertLessEqual(width["learned"], width["uniform"] * 1.02, judge)

    def test_the_textbook_rule_loses_to_uniform_against_a_flattering_judge(self):
        # Documented failure, kept as a test so nobody "simplifies" back to it.
        w = {}
        for design in ("uniform", "textbook"):
            widths = []
            for r in range(150):
                rng = random.Random(f"tb:{design}:{r}")
                widths.append(ac.active_mean(*two_periods(rng, "flattering", 2000, 0.05,
                                                          design)).width)
            w[design] = sum(widths) / len(widths)
        self.assertGreater(w["textbook"], 1.3 * w["uniform"])


class Anytime(unittest.TestCase):

    def _stream(self, rng, n, judge, budget=0.1, floor=0.02):
        items = traffic(rng, n, judge)
        pred = [f for _, f in items]
        pis = ac.inclusion_probabilities([0.3 if f == 0.5 else 0.15 for f in pred], budget,
                                         floor, mix=0.0)
        s, l = label(rng, items, pis)
        return pred, pis, s, l

    def test_the_truth_survives_continuous_monitoring(self):
        """The process at the true mean is the one Ville's inequality protects: if it never
        crosses, the sequence never excluded theta at any of the n looks."""
        for judge in ("flattering", "confidently wrong"):
            ever = 0
            for r in range(200):
                rng = random.Random(f"any:{judge}:{r}")
                book = ac.ActiveCapitalProcess(THETA, alpha=0.05)
                for f, p, s, y in zip(*self._stream(rng, 1500, judge)):
                    z_min, z_max = ac._z_range(f, p, 1.0)
                    book.update(f + ((y - f) / p if s else 0.0), z_min, z_max)
                    if book.rejected:
                        ever += 1
                        break
            self.assertLessEqual(ever / 200, 0.07, judge)

    def test_the_sequence_contains_the_truth_and_narrows(self):
        rng = random.Random(11)
        cs = ac.ActiveConfidenceSequence(alpha=0.05, grid=100)
        widths = []
        for i, (f, p, s, y) in enumerate(zip(*self._stream(rng, 4000, "calibrated"))):
            lo, hi = cs.observe(f, p, s, y)
            if i in (499, 3999):
                widths.append(hi - lo)
        self.assertTrue(lo <= THETA <= hi)
        self.assertLess(widths[1], widths[0])
        self.assertLess(widths[1], 0.35)
        self.assertGreater(cs.n_labels, 0)
        self.assertLess(cs.lam, 1.0 + 1e-12)

    def test_the_asymptotic_sequence_holds_once_it_has_labels(self):
        ever = 0
        runs = 150
        for r in range(runs):
            rng = random.Random(f"asym:{r}")
            pred, pi, s, l = two_periods(rng, "flattering", 3000, 0.05, "learned")
            cs = ac.ActiveConfidenceSequence(alpha=0.05, grid=2)   # grid unused here
            for row in zip(pred, pi, s, l):
                lo, hi = cs.observe(*row)
                alo, ahi = cs.asymptotic_bounds()
                if not alo <= THETA <= ahi:
                    ever += 1
                    break
        self.assertLessEqual(ever / runs, 0.08)

    def test_the_asymptotic_sequence_says_nothing_before_its_labels(self):
        cs = ac.ActiveConfidenceSequence(grid=2)
        for _ in range(500):
            cs.observe(1.0, 0.5, False)
        self.assertEqual(cs.asymptotic_bounds(), (0.0, 1.0))

    def test_the_asymptotic_radius_is_a_modest_premium_over_one_look(self):
        # The price of reading it every day, at the length it is tuned for.
        ratio = ac.asymptotic_radius(2000, 0.05) / 1.96 * math.sqrt(2000)
        self.assertGreater(ratio, 1.2)
        self.assertLess(ratio, 2.0)

    def test_wealth_stays_positive_at_the_extremes(self):
        # A label of 1 on an UNSUPPORTED verdict sampled at the floor: Z = 1/floor.
        book = ac.ActiveCapitalProcess(0.5)
        for _ in range(50):
            z_min, z_max = ac._z_range(0.0, 0.02, 1.0)
            book.update(z_max, z_min, z_max)
            book.update(z_min, z_min, z_max)
        self.assertTrue(math.isfinite(book._log_up) and math.isfinite(book._log_down))


class WeightedConformal(unittest.TestCase):

    def test_equal_weights_reproduce_the_unweighted_quantile(self):
        rng = random.Random(2)
        for n in (9, 19, 50, 200):
            scores = [rng.random() for _ in range(n)]
            for alpha in (0.1, 0.2):
                self.assertEqual(
                    ac.weighted_conformal_quantile(scores, [1.0] * n, alpha, 1.0),
                    conformal_quantile(scores, alpha), (n, alpha))

    def test_a_heavy_test_point_refuses_rather_than_guessing(self):
        self.assertEqual(ac.weighted_conformal_quantile([0.1, 0.2], [1, 1], 0.1, 50.0),
                         math.inf)

    def _calibrations(self, judge, runs=60):
        naive, weighted = [], []
        for r in range(runs):
            rng = random.Random(f"wc:{judge}:{r}")
            items = claim_traffic(rng, 1500, judge)
            pis = ac.inclusion_probabilities([ac.judge_uncertainty(f) for _, _, f in items],
                                             0.15, floor=0.02, mix=0.0)
            chosen = [(a, p) for (a, _, _), p in zip(items, pis) if rng.random() < p]
            test = [a for a, _, _ in claim_traffic(rng, 1500, judge)]
            answers = [a for a, _ in chosen]
            naive.append(fa.evaluate(test, fa.calibrate_strict(answers, 0.1).threshold)
                         ["all_supported"])
            cert = ac.calibrate_strict_weighted(answers, [p for _, p in chosen], 0.1, 0.02)
            weighted.append(fa.evaluate(test, cert.threshold)["all_supported"])
        return sum(naive) / runs, sum(weighted) / runs

    def test_reweighting_keeps_the_promise_where_the_naive_threshold_breaks_it(self):
        naive, weighted = self._calibrations("confidently wrong")
        # The judge sends easy answers to review, so the naive calibration set is too clean
        # and its threshold too low for the traffic it will filter.
        self.assertLess(naive, 0.80)
        self.assertGreaterEqual(weighted, 0.88)

    def test_reweighting_holds_under_a_well_behaved_judge_too(self):
        _, weighted = self._calibrations("calibrated")
        self.assertGreaterEqual(weighted, 0.88)


class Cli(unittest.TestCase):

    def test_an_export_becomes_an_interval_a_threshold_and_a_design(self):
        rng = random.Random(9)
        items = claim_traffic(rng, 1200, "calibrated")
        pis = [0.5 if f == 0.5 else 0.1 for _, _, f in items]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for (claims, y, f), p in zip(items, pis):
                    row = {"prediction": f, "pi": p, "selected": rng.random() < p}
                    if row["selected"]:
                        row["supported"] = bool(y)
                        row["claims"] = [{"score": c.score, "supported": c.supported}
                                         for c in claims]
                    fh.write(json.dumps(row) + "\n")
            out = StringIO()
            with redirect_stdout(out):
                code = ac.main([path, "--budget", "0.1", "--json", os.path.join(tmp, "o.json")])
            with open(os.path.join(tmp, "o.json"), encoding="utf-8") as fh:
                report = json.load(fh)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Anytime-valid", text)
        self.assertIn("recall.rag.factuality:", text)
        self.assertIn("pi-partial:", text)
        self.assertLessEqual(report["anytime"]["lo"], report["fixed"]["estimate"])

    def test_an_unlabelled_selected_row_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"prediction": 1.0, "pi": 0.1, "selected": True}) + "\n")
            with self.assertRaises(ValueError):
                ac.load_review_export(path)


if __name__ == "__main__":
    unittest.main()
