package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;

import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import java.util.Random;
import org.junit.jupiter.api.Test;

/**
 * The statistics are {@link AnytimeRateMonitor}'s, pinned against eval/sequential.py by
 * {@link CoverageMonitorTest}. What is tested here is what is specific to the claim
 * certificate: when it is allowed to watch, and what counts as a breach of it.
 */
class FactualityMonitorTest {

    private static final double ALPHA = 0.10;

    private static FactualityMonitor monitor(boolean enabled, double threshold, boolean judge,
                                             int warmup) {
        RecallProperties props = new RecallProperties(null, null, null, null, null, null, null,
                new RecallProperties.Rag(
                        new RecallProperties.Rag.Judge(judge, 5), null, null, null,
                        new RecallProperties.Rag.Factuality(enabled, ALPHA, threshold, 12, 10,
                                0.05, warmup)),
                null);
        return new FactualityMonitor(props, new SimpleMeterRegistry());
    }

    private static FactualityMonitor live() {
        return monitor(true, 0.5, true, 0);
    }

    private static void feed(FactualityMonitor m, Random rng, double missRate, int n) {
        for (int i = 0; i < n; i++) {
            m.observe((rng.nextDouble() < missRate
                    ? Judgment.Verdict.UNSUPPORTED : Judgment.Verdict.SUPPORTED).toJudgment());
        }
    }

    @Test
    void watchesOnlyACalibratedFilterWithAJudgeToAsk() {
        assertThat(live().enabled()).isTrue();
        assertThat(monitor(true, -1, true, 0).enabled()).as("observe mode promises nothing").isFalse();
        assertThat(monitor(true, 0.5, false, 0).enabled()).as("no judge, no signal").isFalse();
        assertThat(monitor(false, 0.5, true, 0).enabled()).isFalse();

        FactualityMonitor inert = monitor(true, -1, true, 0);
        inert.observe(Judgment.Verdict.UNSUPPORTED.toJudgment());
        assertThat(inert.observedCount()).isZero();
    }

    @Test
    void partialIsABreachHereThoughNotForCoverage() {
        // "Every remaining claim is supported" is broken by one unsupported claim, which is
        // what PARTIAL reports. The coverage monitor lets PARTIAL pass; this one cannot.
        FactualityMonitor m = live();
        m.observe(Judgment.Verdict.PARTIAL.toJudgment());
        m.observe(Judgment.Verdict.SUPPORTED.toJudgment());
        assertThat(m.missRate()).isEqualTo(0.5);
    }

    @Test
    void aCertificateKeptIsQuiet() {
        FactualityMonitor m = live();
        feed(m, new Random(1), 0.08, 2000);
        assertThat(m.alarming()).isFalse();
    }

    @Test
    void theDistractorRegimeIsCaught() {
        // The offline simulation's distractor shift: a 90% promise delivering ~52%.
        FactualityMonitor m = live();
        Random rng = new Random(2);
        int step = 0;
        while (!m.alarming() && step < 2000) {
            feed(m, rng, 0.48, 1);
            step++;
        }
        assertThat(m.alarming()).isTrue();
        assertThat(step).as("answers before the alarm").isLessThan(60);
        assertThat(m.lower()).isGreaterThan(ALPHA);
    }

    @Test
    void keptTextIsWhatTheReaderIsLeftWith() {
        String answer = "Alpha holds [1]. Beta was invented. Gamma holds [2].";
        ClaimReport r = new ClaimReport(ALPHA, 0.5, true, List.of(
                new ClaimReport.Claim(0, 16, 0.9, true),
                new ClaimReport.Claim(17, 35, 0.1, false),
                new ClaimReport.Claim(36, 52, 0.8, true)));
        assertThat(r.keptText(answer)).isEqualTo("Alpha holds [1]. Gamma holds [2].");

        ClaimReport all = new ClaimReport(ALPHA, 0.5, true, List.of(
                new ClaimReport.Claim(0, answer.length(), 0.1, false)));
        assertThat(all.keptText(answer)).isEmpty();
    }
}
