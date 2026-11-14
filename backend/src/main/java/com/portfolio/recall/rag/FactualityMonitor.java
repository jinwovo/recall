package com.portfolio.recall.rag;

import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Watches whether the claim certificate from {@link ClaimVerifier} is still true
 * (docs/adr/0017, using the ADR 0016 machinery).
 *
 * <p>The certificate promises {@code P(some claim left standing is unsupported) <= alpha},
 * and like every conformal threshold it holds only while serving looks like calibration. The
 * 2026 robustness study (arXiv 2603.16817) is specific about how that fails: distractor
 * passages that make false claims look entailed, which the offline simulation reproduces as
 * a 90% promise delivering 52%. The threshold does not move when that happens, so without
 * this the failure is silent.
 *
 * <h2>What is observed</h2>
 *
 * <p>One observation per <em>certified</em> answer — calibrated threshold, every claim scored —
 * and it is the judge's verdict on the text the reader was left with, not on the full
 * answer. Judging the full answer would count the claims the filter correctly removed as
 * failures of the filter. When nothing was withheld the two texts are the same and the
 * ordinary verdict is reused; otherwise {@code RagService} asks the judge once more about the
 * remaining text, off the response path.
 *
 * <p>A miss is anything short of {@code SUPPORTED}. That is stricter than the coverage
 * monitor, which lets {@code PARTIAL} pass, and it has to be: this promise is about every
 * remaining claim, and "partially supported" says at least one of them is not.
 *
 * <p>The caveats {@link CoverageMonitor} states apply unchanged — the judge is a biased
 * instrument (docs/adr/0015), and the monitored stream excludes judge timeouts and cache hits
 * — so this too is a smoke detector, not a thermometer.
 *
 * <p>Inert unless the filter is enabled and calibrated, and the judge is on.
 */
@Component
public class FactualityMonitor {

    private static final Logger log = LoggerFactory.getLogger(FactualityMonitor.class);

    private final boolean enabled;
    private final double alpha;
    private final MeterRegistry meters;
    private final AnytimeRateMonitor rate;

    public FactualityMonitor(RecallProperties props, MeterRegistry meters) {
        RecallProperties.Rag.Factuality f = props.rag().factuality();
        RecallProperties.Rag.Judge judge = props.rag().judge();
        this.alpha = f.alpha();
        this.meters = meters;
        this.enabled = f.enabled() && f.calibrated() && judge != null && judge.enabled()
                && alpha > 0.0 && alpha < 1.0;
        if (f.enabled() && f.calibrated() && !this.enabled) {
            log.info("Factuality monitoring off: the claim filter is calibrated but the judge "
                    + "is disabled, so nothing can say whether the certificate still holds.");
        }
        this.rate = new AnytimeRateMonitor(alpha,
                f.monitorAlpha() > 0 ? f.monitorAlpha() : 0.05, f.monitorWarmup());
        meters.gauge("recall.rag.factuality.miss_rate", this, FactualityMonitor::missRate);
        meters.gauge("recall.rag.factuality.alarming", this, m -> m.alarming() ? 1.0 : 0.0);
    }

    public boolean enabled() {
        return enabled;
    }

    /** Record the judge's verdict on the claims a certified answer kept. */
    public void observe(Judgment keptVerdict) {
        if (!enabled || keptVerdict == null) {
            return;
        }
        if (rate.observe(keptVerdict.verdict() != Judgment.Verdict.SUPPORTED)) {
            meters.counter("recall.rag.factuality.alarm").increment();
            log.warn("Claim certificate breached: answers the filter certified are being judged "
                            + "not fully supported more often than it promises, and the evidence "
                            + "has ruled the promise out. promised {}, observed {} over {} "
                            + "certified answers, anytime-valid interval [{}, {}]. The claim "
                            + "scores no longer look like calibration — re-label a fresh sample "
                            + "and re-run eval/factuality.py. This is the judge's view "
                            + "(docs/adr/0015), not ground truth.",
                    CoverageMonitor.fmt(alpha), CoverageMonitor.fmt(missRate()), observedCount(),
                    CoverageMonitor.fmt(rate.lower()), CoverageMonitor.fmt(rate.upper()));
        }
    }

    public boolean alarming() {
        return rate.alarming();
    }

    public long observedCount() {
        return rate.observedCount();
    }

    public double missRate() {
        return rate.rate();
    }

    public double lower() {
        return rate.lower();
    }
}
