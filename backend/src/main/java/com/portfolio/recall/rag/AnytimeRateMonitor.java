package com.portfolio.recall.rag;

import java.util.concurrent.atomic.AtomicBoolean;

/**
 * An anytime-valid, one-sided alarm on a miss rate, shared by {@link CoverageMonitor}
 * (docs/adr/0016) and {@link FactualityMonitor} (docs/adr/0017).
 *
 * <p>Both defend a promise of the form {@code P(miss) <= alpha} on a stream inspected after
 * every observation — exactly the arrangement a fixed-sample test cannot survive
 * (docs/adr/0012). The miss rate is tracked with a betting confidence sequence (Waudby-Smith
 * &amp; Ramdas, JRSS-B 2024), one capital process per grid point, valid at every sample size
 * at once. {@link #observe} returns true exactly once: on the observation that rules the
 * promised rate out, upward. What counts as a miss, and what to say when the alarm fires,
 * belongs to the caller.
 */
final class AnytimeRateMonitor {

    /** Kept identical to {@code eval/sequential.py}; {@code CoverageMonitorTest} pins them. */
    static final double BET_FRACTION = 0.5;
    static final double HEDGE = 0.5;
    static final int GRID = 200;

    private final double alpha;
    private final int warmup;
    private final double[] points = new double[GRID];
    private final CapitalProcess[] books = new CapitalProcess[GRID];
    private final AtomicBoolean alarming = new AtomicBoolean(false);

    private long observed;
    private long misses;

    /**
     * @param alpha     the promised miss rate being defended
     * @param testAlpha the confidence sequence's own error budget over the whole stream
     * @param warmup    observations before the alarm may fire (they still count)
     */
    AnytimeRateMonitor(double alpha, double testAlpha, int warmup) {
        this.alpha = alpha;
        this.warmup = Math.max(0, warmup);
        for (int j = 0; j < GRID; j++) {
            // Cell midpoints, so 0 and 1 are never hypothesised and the stake bounds stay
            // finite. Same construction as the Python ConfidenceSequence.
            points[j] = (j + 0.5) / GRID;
            books[j] = new CapitalProcess(points[j], testAlpha);
        }
    }

    /**
     * Record one outcome. Returns true only on the observation that latches the alarm.
     *
     * <p>Synchronized: the capital processes are shared mutable state, and the martingale
     * argument requires each stake to be a function of the observations that preceded it.
     * Interleaved updates would break that; the cost is a few hundred multiplications under
     * a lock, next to an LLM round trip.
     */
    synchronized boolean observe(boolean miss) {
        double x = miss ? 1.0 : 0.0;
        observed++;
        misses += (long) x;
        for (CapitalProcess book : books) {
            book.update(x);
        }
        if (observed < warmup) {
            return false;
        }
        // Latched: the sequence is valid at every sample size, so once alpha is ruled out
        // the evidence does not stop existing because a later observation went well.
        return lower() > alpha && alarming.compareAndSet(false, true);
    }

    boolean alarming() {
        return alarming.get();
    }

    /** The promise is being kept with room to spare. A cost signal, never an alarm. */
    synchronized boolean overCovering() {
        return observed >= warmup && observed > 0 && upper() < alpha;
    }

    synchronized long observedCount() {
        return observed;
    }

    /** Realised miss rate, or 0 before anything is observed. */
    synchronized double rate() {
        return observed == 0 ? 0.0 : (double) misses / observed;
    }

    /** Lower end of the anytime-valid interval on the miss rate. */
    synchronized double lower() {
        for (int j = 0; j < GRID; j++) {
            if (!books[j].rejected()) {
                return points[j];
            }
        }
        return 0.0;                             // every hypothesis rejected — widen out
    }

    /** Upper end of the anytime-valid interval on the miss rate. */
    synchronized double upper() {
        for (int j = GRID - 1; j >= 0; j--) {
            if (!books[j].rejected()) {
                return points[j];
            }
        }
        return 1.0;
    }

    /**
     * Wealth from betting against one hypothesised mean, in log space.
     *
     * <p>A direct port of {@code CapitalProcess} in {@code eval/sequential.py}. Two books are
     * kept — one profiting when observations run above {@code m}, one below — and under the
     * null both are non-negative martingales starting at 1, so Ville's inequality bounds the
     * chance either ever reaches the rejection level, at any stopping time the observer
     * likes. The port is only worth what its agreement with the Python is worth, which is
     * why {@code CoverageMonitorTest} pins it against generated golden vectors.
     */
    static final class CapitalProcess {

        private final double m;
        private final double logReject;
        private final double alpha;

        private long n;
        private double sum;
        private double logUp;
        private double logDown;
        private double runningMean = 0.5;       // uniform prior (1/2, 1/4), which is what
        private double sqDev;                   // keeps the first few bets cautious
        private double var = 0.25;

        CapitalProcess(double m, double alpha) {
            if (!(m > 0.0 && m < 1.0)) {
                throw new IllegalArgumentException("hypothesised mean must be in (0, 1): " + m);
            }
            this.m = m;
            this.alpha = alpha;
            this.logReject = Math.log(1.0 / (alpha * HEDGE));
        }

        void update(double x) {
            if (!(x >= 0.0 && x <= 1.0)) {
                throw new IllegalArgumentException("observations must lie in [0, 1]: " + x);
            }
            long t = n + 1;
            // Predictable stake: uses only what was known before x arrived, which is the
            // condition the martingale argument rests on.
            double stake = Math.sqrt(2.0 * Math.log(2.0 / alpha)
                    / (var * t * Math.log(1.0 + t)));
            double edge = x - m;
            double up = Math.min(stake, BET_FRACTION / m);
            double down = Math.min(stake, BET_FRACTION / (1.0 - m));
            logUp += Math.log1p(up * edge);
            logDown += Math.log1p(-down * edge);

            // Advance the estimates the *next* bet will use. The squared deviation is taken
            // against the mean as it stood before x arrived, keeping both functions of the
            // past only.
            n = t;
            sum += x;
            sqDev += (x - runningMean) * (x - runningMean);
            runningMean = (0.5 + sum) / (1.0 + t);
            var = (0.25 + sqDev) / (1.0 + t);
        }

        double logWealth() {
            return Math.max(logUp, logDown);
        }

        boolean rejected() {
            return logWealth() >= logReject;
        }
    }
}
