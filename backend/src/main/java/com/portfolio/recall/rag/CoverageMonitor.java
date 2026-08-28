package com.portfolio.recall.rag;

import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Watches whether the coverage certificate from {@code ConformalSetSizer} is still true
 * (docs/adr/0016).
 *
 * <p>{@link ConformalSetSizer} deploys a threshold calibrated offline, and its guarantee —
 * {@code P(the kept passages contain a relevant document) >= 1 - alpha} — holds only while
 * calibration and serving queries are draws from the same distribution. A live corpus stops
 * satisfying that within a week: documents are ingested, query mix moves, someone swaps the
 * embedding model. Measured offline over 200 streams of 3,000 queries, a frozen threshold
 * delivers 45.9% coverage against a promised 90%. The failure is silent — the config is
 * unchanged, the dashboards are green, and the promise has quietly stopped being true.
 *
 * <p>This class makes it audible. It is deliberately <em>not</em> the adaptive controller
 * from {@code eval/adaptive.py}; see "why this monitors rather than corrects" below.
 *
 * <h2>Why the test has to be anytime-valid</h2>
 *
 * <p>The obvious implementation is a binomial test on the miscoverage rate, checked
 * periodically. That is invalid here for exactly the reason
 * <a href="../../../../../../../../docs/adr/0012-anytime-valid-evaluation.md">ADR 0012</a>
 * documents: a fixed-sample interval promises to contain the truth 95% of the time when it
 * is inspected <em>once</em>, on a <em>finished</em> sample. This stream is inspected after
 * every single query and alarms on the first excursion, which is continuous inspection with
 * optional stopping — the arrangement under which a 95% interval was measured to miss
 * 30-35% of the time. An alarm built that way would cry wolf several times a week.
 *
 * <p>So the miscoverage rate is tracked with a betting confidence sequence (Waudby-Smith
 * &amp; Ramdas, JRSS-B 2024): one capital process per grid point, each a non-negative
 * martingale under its own null, bounded by Ville's inequality <em>at every sample size
 * simultaneously</em>. Checking it after every query costs nothing in validity, which is the
 * entire point.
 *
 * <h2>The alarm is one-sided, and that is not a shortcut</h2>
 *
 * <p>The certificate promises {@code miscoverage <= alpha}. Only one direction breaks it.
 * Firing whenever alpha merely leaves the interval would alarm on a system that is covering
 * <em>better</em> than promised — and conformal prediction is conservative by construction,
 * so that is the ordinary state of a healthy deployment, not an incident. The breach alarm
 * therefore fires when the interval's <em>lower</em> end rises above alpha: the evidence has
 * ruled out the promised rate in the direction that costs something.
 *
 * <p>The other direction is still worth knowing and is reported separately as
 * {@link #overCovering()} — sets larger than the guarantee requires, which is prompt tokens
 * rather than hallucinations. It is a gauge, not a warning, because nobody should be paged
 * for it.
 *
 * <h2>What the coverage signal actually is, and what that costs</h2>
 *
 * <p>Serving cannot compute a true coverage outcome: that needs to know which passage was
 * relevant, which is what the gold labels supply offline and nothing supplies online. The
 * signal used instead is the post-hoc groundedness judge — an {@code UNSUPPORTED} verdict is
 * counted as a miss, since an answer the judge cannot tie back to the retrieved passages is
 * the failure the conformal set exists to prevent.
 *
 * <p>Two honest consequences follow, and neither is repaired by anything in this class:
 *
 * <ul>
 *   <li><strong>The signal is a biased instrument.</strong>
 *       <a href="../../../../../../../../docs/adr/0015-prediction-powered-inference.md">ADR
 *       0015</a> measured a judge's own score covering the truth 0% of the time. This alarm
 *       therefore tracks the <em>judge's</em> miscoverage rate, not the true one, and a
 *       drift in the judge is indistinguishable here from a drift in retrieval. It is a
 *       smoke detector, not a thermometer: worth acting on, not worth quoting.</li>
 *   <li><strong>The sample is not all traffic.</strong> Judging is fail-open, skipped on
 *       abstentions, and skipped on semantic-cache hits, so the monitored stream is a
 *       subpopulation of served queries. The guarantee is over the queries it observed,
 *       which is a narrower statement than it looks.</li>
 * </ul>
 *
 * <h2>Why this monitors rather than corrects</h2>
 *
 * <p>{@code eval/adaptive.py} closes the loop, driving the level from realised miscoverage
 * with a guarantee that survives arbitrary shift. It stays offline because it needs a
 * nonconformity score per step to recompute its quantile, and serving has no gold label to
 * derive one from. Driving the threshold directly on a binary verdict would look like the
 * same algorithm while quietly dropping the property that makes it work — the containment
 * argument needs an empty set that always misses, and the shortest set this pipeline can
 * return is one passage. Shipping that would be a guarantee-shaped object with no guarantee
 * in it, which is the failure this repository was built to catch.
 *
 * <p>Disabled by default. With sizing off or no alpha configured it observes nothing.
 */
@Component
public class CoverageMonitor {

    private static final Logger log = LoggerFactory.getLogger(CoverageMonitor.class);

    /** Kept identical to {@code eval/sequential.py}; {@code CoverageMonitorTest} pins them. */
    private static final double BET_FRACTION = 0.5;
    private static final double HEDGE = 0.5;
    private static final int GRID = 200;

    private final boolean enabled;
    private final double alpha;                 // the coverage level being defended
    private final double testAlpha;             // the confidence sequence's own error budget
    private final int warmup;
    private final MeterRegistry meters;

    private final double[] points = new double[GRID];
    private final CapitalProcess[] books = new CapitalProcess[GRID];
    private final AtomicBoolean alarming = new AtomicBoolean(false);

    private long observed;
    private long misses;

    public CoverageMonitor(RecallProperties props, MeterRegistry meters) {
        RecallProperties.Rag.Conformal conformal = props.rag().conformal();
        this.alpha = conformal.alpha();
        this.testAlpha = conformal.monitorAlpha() > 0 ? conformal.monitorAlpha() : 0.05;
        this.warmup = Math.max(0, conformal.monitorWarmup());
        this.meters = meters;
        // Monitoring a certificate that was never issued is noise, and an alpha outside
        // (0, 1) is an unconfigured one rather than a lenient one.
        this.enabled = conformal.monitorEnabled() && conformal.enabled()
                && alpha > 0.0 && alpha < 1.0;
        if (conformal.monitorEnabled() && !this.enabled) {
            log.info("Coverage monitoring off: sizing enabled={}, alpha={}.",
                    conformal.enabled(), alpha);
        }
        for (int j = 0; j < GRID; j++) {
            // Cell midpoints, so 0 and 1 are never hypothesised and the stake bounds stay
            // finite. Same construction as the Python ConfidenceSequence.
            points[j] = (j + 0.5) / GRID;
            books[j] = new CapitalProcess(points[j], testAlpha);
        }
        meters.gauge("recall.rag.coverage.miscoverage", this, CoverageMonitor::miscoverage);
        meters.gauge("recall.rag.coverage.alarming", this, m -> m.alarming.get() ? 1.0 : 0.0);
        meters.gauge("recall.rag.coverage.overcovering", this, m -> m.overCovering() ? 1.0 : 0.0);
    }

    public boolean enabled() {
        return enabled;
    }

    /**
     * Record one judged answer. {@code UNSUPPORTED} counts as a miss; {@code PARTIAL} does
     * not, because the passages did carry something the answer stood on and calling that a
     * coverage failure would inflate the rate the certificate is being held to.
     *
     * <p>Synchronized: the capital processes are mutable state shared across request
     * threads, and the martingale argument requires each stake to be a function of the
     * observations that preceded it. Interleaved updates would break that, and the cost is
     * a few hundred multiplications under a lock, next to an LLM round trip.
     */
    public synchronized void observe(Judgment judgment) {
        if (!enabled || judgment == null) {
            return;
        }
        double miss = judgment.verdict() == Judgment.Verdict.UNSUPPORTED ? 1.0 : 0.0;
        observed++;
        misses += (long) miss;
        for (CapitalProcess book : books) {
            book.update(miss);
        }
        if (observed < warmup) {
            return;
        }
        boolean breached = lower() > alpha;
        if (breached && alarming.compareAndSet(false, true)) {
            // Latched: the sequence is valid at every sample size, so once alpha is ruled
            // out the evidence does not stop existing because a later query went well.
            meters.counter("recall.rag.coverage.alarm").increment();
            log.warn("Coverage certificate breached: measured miscoverage is above what it "
                            + "promises, and the evidence has ruled the promise out. "
                            + "promised miscoverage {}, observed {} over {} judged answers, "
                            + "anytime-valid interval [{}, {}]. The conformal threshold was "
                            + "calibrated on a distribution the traffic no longer matches — "
                            + "re-run eval/calibrate.py. Note this is the judge's view of "
                            + "coverage (docs/adr/0015), not ground truth.",
                    fmt(alpha), fmt(miscoverage()), observed, fmt(lower()), fmt(upper()));
        }
    }

    /**
     * True once the evidence has ruled out the promised miscoverage rate, upward.
     *
     * <p>Latched: the sequence is valid at every sample size, so once alpha has been
     * excluded the evidence does not stop existing because the next few queries went well.
     * Clearing it is a deploy-time decision — recalibrate, then restart — not a runtime one.
     */
    public boolean alarming() {
        return alarming.get();
    }

    /**
     * True when the evidence says miscoverage is <em>below</em> alpha: the promise is being
     * kept with room to spare, which means context sets larger than it requires. Reported,
     * never alarmed on — this is a cost signal, not a correctness one.
     */
    public synchronized boolean overCovering() {
        return observed >= warmup && observed > 0 && upper() < alpha;
    }

    public long observedCount() {
        return observed;
    }

    /** Realised miscoverage over judged answers, or 0 before any arrive. */
    public double miscoverage() {
        return observed == 0 ? 0.0 : (double) misses / observed;
    }

    /** Lower end of the anytime-valid interval on the miscoverage rate. */
    public synchronized double lower() {
        double lo = Double.NaN;
        for (int j = 0; j < GRID; j++) {
            if (!books[j].rejected()) {
                lo = points[j];
                break;
            }
        }
        return Double.isNaN(lo) ? 0.0 : lo;     // every hypothesis rejected — widen out
    }

    /** Upper end of the anytime-valid interval on the miscoverage rate. */
    public synchronized double upper() {
        double hi = Double.NaN;
        for (int j = GRID - 1; j >= 0; j--) {
            if (!books[j].rejected()) {
                hi = points[j];
                break;
            }
        }
        return Double.isNaN(hi) ? 1.0 : hi;
    }

    /** Whether a hypothesised miscoverage rate is still standing. */
    public synchronized boolean contains(double m) {
        return lower() <= m && m <= upper();
    }

    private static String fmt(double value) {
        return String.format("%.4f", value);
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
