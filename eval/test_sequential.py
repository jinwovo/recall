"""Tests for anytime-valid evaluation (eval/sequential.py).

The claim being tested is not "the interval is roughly right at the end" — a fixed-N
interval manages that. It is that the interval may be looked at after *every* query and
still keep its coverage, which is what licenses stopping early. So the central test is a
simulation that inspects continuously and counts how often the truth ever escapes, run
against both methods so the difference is measured rather than asserted.

Run: `python -m unittest discover -s eval -p "test_*.py"` (standard library only).
"""
import math
import random
import unittest

import sequential as seq

# Enough streams to separate ~4% from ~30% without being slow. Each stream is O(1) per
# observation because the gate only ever tests one hypothesised mean.
STREAMS = 600
STREAM_LENGTH = 150


def naive_interval(values: list[float]) -> tuple[float, float]:
    """The fixed-N interval this replaces: normal approximation, recomputed on demand."""
    n = len(values)
    if n < 2:
        return (0.0, 1.0)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = 1.959963985 * math.sqrt(var / n)
    return (max(0.0, mean - half), min(1.0, mean + half))


def escapes_under_peeking(sampler, truth: float, streams: int, length: int,
                          method: str, seed: int) -> float:
    """Share of streams where the true mean is ever outside an interval being watched."""
    rng = random.Random(seed)
    escaped = 0
    for _ in range(streams):
        draws = [sampler(rng) for _ in range(length)]
        if method == "sequential":
            book = seq.CapitalProcess(truth)
            for x in draws:
                book.update(x)
                if book.rejected:               # truth excluded from the confidence sequence
                    escaped += 1
                    break
        else:
            for t in range(2, length + 1):
                lo, hi = naive_interval(draws[:t])
                if not lo <= truth <= hi:
                    escaped += 1
                    break
    return escaped / streams


class AnytimeValidityTest(unittest.TestCase):
    """The guarantee, and the failure it replaces."""

    def test_confidence_sequence_holds_up_under_continuous_inspection(self):
        # Ville's inequality bounds the chance the truth is *ever* excluded by alpha,
        # across the whole stream, however the observer chooses to stop.
        rate = escapes_under_peeking(lambda r: r.betavariate(2, 3), 0.4,
                                     STREAMS, STREAM_LENGTH, "sequential", seed=11)
        self.assertLessEqual(rate, 0.05 + 0.02, f"escape rate {rate:.3f} exceeds alpha")

    def test_the_fixed_n_interval_does_not(self):
        # Same streams, same nominal 95%, checked the way people actually read CI logs.
        naive = escapes_under_peeking(lambda r: r.betavariate(2, 3), 0.4,
                                      STREAMS, STREAM_LENGTH, "naive", seed=11)
        anytime = escapes_under_peeking(lambda r: r.betavariate(2, 3), 0.4,
                                        STREAMS, STREAM_LENGTH, "sequential", seed=11)
        self.assertGreater(naive, 0.15, "expected peeking to break the fixed-N interval")
        self.assertGreater(naive, 3 * anytime)

    def test_validity_holds_for_a_discrete_skewed_metric(self):
        # Reciprocal rank is the awkward case: three atoms, heavy skew, bounded. The
        # betting construction assumes only boundedness, so it should be unbothered.
        support = [1.0, 0.5, 0.25]
        weights = [0.6, 0.3, 0.1]
        truth = sum(s * w for s, w in zip(support, weights))
        rate = escapes_under_peeking(lambda r: r.choices(support, weights)[0], truth,
                                     STREAMS, STREAM_LENGTH, "sequential", seed=23)
        self.assertLessEqual(rate, 0.05 + 0.02, f"escape rate {rate:.3f} exceeds alpha")


class CapitalProcessTest(unittest.TestCase):

    def test_wealth_starts_at_one_and_nothing_is_decided(self):
        book = seq.CapitalProcess(0.5)
        self.assertEqual(book.log_wealth, 0.0)
        self.assertFalse(book.rejected)
        self.assertEqual(book.direction, 0)

    def test_evidence_above_the_hypothesis_points_up(self):
        book = seq.CapitalProcess(0.3)
        for _ in range(60):
            book.update(0.95)
        self.assertTrue(book.rejected)
        self.assertEqual(book.direction, 1)

    def test_evidence_below_the_hypothesis_points_down(self):
        book = seq.CapitalProcess(0.8)
        for _ in range(60):
            book.update(0.05)
        self.assertTrue(book.rejected)
        self.assertEqual(book.direction, -1)

    def test_observations_at_the_hypothesis_never_decide(self):
        book = seq.CapitalProcess(0.5)
        for _ in range(500):
            book.update(0.5)
        self.assertFalse(book.rejected)
        self.assertLessEqual(book.log_wealth, 0.0 + 1e-9)

    def test_tracks_the_running_mean(self):
        book = seq.CapitalProcess(0.5)
        for x in (0.2, 0.4, 0.6, 0.8):
            book.update(x)
        self.assertAlmostEqual(book.mean, 0.5, places=12)
        self.assertEqual(book.n, 4)

    def test_rejects_out_of_range_input(self):
        with self.assertRaises(ValueError):
            seq.CapitalProcess(0.0)
        with self.assertRaises(ValueError):
            seq.CapitalProcess(1.0)
        with self.assertRaises(ValueError):
            seq.CapitalProcess(0.5).update(1.5)


class ConfidenceSequenceTest(unittest.TestCase):

    def test_interval_brackets_the_sample_mean_and_narrows(self):
        rng = random.Random(4)
        draws = [rng.betavariate(5, 2) for _ in range(200)]
        cs = seq.ConfidenceSequence(grid=100)
        cs.extend(draws[:20])
        early = cs.bounds
        cs.extend(draws[20:])
        late = cs.bounds
        self.assertLessEqual(late[0], cs.mean)
        self.assertGreaterEqual(late[1], cs.mean)
        self.assertLess(late[1] - late[0], early[1] - early[0])

    def test_interval_covers_the_truth_it_was_generated_from(self):
        rng = random.Random(8)
        cs = seq.ConfidenceSequence(grid=100)
        cs.extend(rng.betavariate(5, 2) for _ in range(300))
        self.assertTrue(cs.contains(5 / 7))          # Beta(5, 2) has mean 5/7

    def test_starts_uninformative(self):
        cs = seq.ConfidenceSequence(grid=50)
        lo, hi = cs.update(0.5)
        self.assertLess(lo, 0.2)
        self.assertGreater(hi, 0.8)


class SequentialGateTest(unittest.TestCase):

    def test_passes_early_when_the_system_is_clearly_above_the_line(self):
        rng = random.Random(15)
        values = [1.0 if rng.random() < 0.95 else 0.0 for _ in range(300)]
        verdict = seq.SequentialGate(0.60, "recall@5").run(values, seed=1)
        self.assertEqual(verdict.decision, "pass")
        self.assertTrue(verdict.passed)
        self.assertLess(verdict.queries_used, 60)
        self.assertGreater(verdict.saved_fraction, 0.7)

    def test_fails_early_when_it_is_clearly_below(self):
        rng = random.Random(16)
        values = [1.0 if rng.random() < 0.30 else 0.0 for _ in range(300)]
        verdict = seq.SequentialGate(0.85, "recall@5").run(values, seed=1)
        self.assertEqual(verdict.decision, "fail")
        self.assertFalse(verdict.passed)
        self.assertLess(verdict.queries_used, 60)

    def test_a_system_sitting_on_the_line_is_reported_undecided_not_passed(self):
        # The honest outcome: the budget ran out before the evidence arrived. Treating
        # that as a pass is how a gate silently stops gating.
        rng = random.Random(17)
        values = [1.0 if rng.random() < 0.85 else 0.0 for _ in range(120)]
        verdict = seq.SequentialGate(0.85, "recall@5").run(values, seed=1)
        self.assertEqual(verdict.decision, "undecided")
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.queries_used, verdict.queries_available)
        self.assertEqual(verdict.saved, 0)

    def test_type_one_error_stays_within_alpha_at_the_threshold(self):
        # The null case: the true mean is exactly the threshold, so any decision is an
        # error. Ville's bound caps how often that happens across the whole stream.
        wrong = 0
        trials = 400
        for i in range(trials):
            rng = random.Random(2000 + i)
            values = [1.0 if rng.random() < 0.7 else 0.0 for _ in range(120)]
            if seq.SequentialGate(0.7, "m").run(values).decided:
                wrong += 1
        self.assertLessEqual(wrong / trials, 0.08, f"decided wrongly {wrong}/{trials}")

    def test_bigger_gaps_are_settled_sooner(self):
        def stop(rate):
            rng = random.Random(31)
            values = [1.0 if rng.random() < rate else 0.0 for _ in range(400)]
            return seq.SequentialGate(0.5, "m").run(values).queries_used
        self.assertLess(stop(0.95), stop(0.70))

    def test_the_shuffle_seed_is_recorded_and_reproducible(self):
        values = [1.0] * 40 + [0.0] * 40
        first = seq.SequentialGate(0.2, "m").run(values, seed=7)
        second = seq.SequentialGate(0.2, "m").run(values, seed=7)
        self.assertEqual(first.seed, 7)
        self.assertEqual(first.queries_used, second.queries_used)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_file_order_and_shuffled_order_are_different_procedures(self):
        # All the wins first, then all the losses: in file order the early sample is
        # unrepresentative, which is exactly why the shuffle is part of the interface.
        values = [1.0] * 100 + [0.0] * 100
        unshuffled = seq.SequentialGate(0.5, "m").run(values)
        shuffled = seq.SequentialGate(0.5, "m").run(values, seed=3)
        self.assertEqual(unshuffled.decision, "pass")        # decided on a biased prefix
        self.assertNotEqual(unshuffled.queries_used, shuffled.queries_used)


class PairedGateTest(unittest.TestCase):

    def test_detects_a_consistent_win_quickly(self):
        candidate = [1.0] * 200
        baseline = [0.5] * 200
        verdict = seq.paired_gate(candidate, baseline, seed=5)
        self.assertEqual(verdict.decision, "pass")
        self.assertLess(verdict.queries_used, 40)
        self.assertAlmostEqual(verdict.mean, 0.5, places=6)

    def test_detects_a_consistent_loss(self):
        verdict = seq.paired_gate([0.2] * 200, [0.9] * 200, seed=5)
        self.assertEqual(verdict.decision, "fail")
        self.assertLess(verdict.mean, 0)

    def test_two_identical_systems_are_never_declared_different(self):
        values = [0.1, 0.9, 0.5, 1.0, 0.0] * 60
        verdict = seq.paired_gate(values, values, seed=5)
        self.assertEqual(verdict.decision, "undecided")
        self.assertEqual(verdict.mean, 0.0)

    def test_pairing_finds_a_small_shift_a_gate_on_raw_scores_would_miss(self):
        # Per-query scores vary wildly; the difference between the systems does not.
        # Pairing cancels the variance that would otherwise dominate.
        rng = random.Random(12)
        baseline = [rng.betavariate(2, 2) * 0.8 for _ in range(400)]
        candidate = [min(1.0, b + 0.08) for b in baseline]
        verdict = seq.paired_gate(candidate, baseline, seed=9)
        self.assertEqual(verdict.decision, "pass")
        self.assertLess(verdict.queries_used, 200)

    def test_rejects_unpaired_input(self):
        with self.assertRaises(ValueError):
            seq.paired_gate([0.1, 0.2], [0.1])


class ShuffleTest(unittest.TestCase):

    def test_is_a_permutation_and_leaves_the_input_alone(self):
        original = list(range(20))
        out = seq.shuffled(original, seed=1)
        self.assertEqual(sorted(out), original)
        self.assertEqual(original, list(range(20)))

    def test_is_seeded(self):
        self.assertEqual(seq.shuffled(list(range(50)), 4), seq.shuffled(list(range(50)), 4))
        self.assertNotEqual(seq.shuffled(list(range(50)), 4), seq.shuffled(list(range(50)), 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
