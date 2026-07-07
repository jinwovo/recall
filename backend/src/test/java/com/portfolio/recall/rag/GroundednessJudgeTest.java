package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;

class GroundednessJudgeTest {

    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();
    private final List<RetrievedChunk> chunks = List.of(
            new RetrievedChunk("c1", "doc-1", 0, "Spring Boot auto-configures beans.", "spring-docs", "en", 1.0));

    @Test
    void unsupportedWinsOverItsSupportedSubstring() {
        // "UNSUPPORTED" contains "SUPPORTED" — precedence must not misgrade a hallucination.
        assertThat(GroundednessJudge.parse("The answer is UNSUPPORTED."))
                .contains(Judgment.Verdict.UNSUPPORTED.toJudgment());
    }

    @Test
    void parsesVerdictsLeniently() {
        assertThat(GroundednessJudge.parse("SUPPORTED")).contains(Judgment.Verdict.SUPPORTED.toJudgment());
        assertThat(GroundednessJudge.parse("verdict: partial")).contains(Judgment.Verdict.PARTIAL.toJudgment());
        assertThat(GroundednessJudge.parse("I think it's fine")).isEmpty();
        assertThat(GroundednessJudge.parse(null)).isEmpty();
    }

    @Test
    void collectsStreamedTokensAndRecordsMetrics() {
        // Judge models stream too — the verdict may arrive split across tokens.
        GroundednessJudge judge = judge((system, prompt, tier) -> Flux.just("SUPP", "ORTED"), true);

        Optional<Judgment> j = judge.judge("q", chunks, "Spring Boot auto-configures beans [1].").block();

        assertThat(j).contains(Judgment.Verdict.SUPPORTED.toJudgment());
        assertThat(meters.counter("recall.rag.judge.verdicts", "verdict", "supported").count()).isEqualTo(1.0);
        assertThat(meters.summary("recall.rag.groundedness").totalAmount()).isEqualTo(1.0);
    }

    @Test
    void failsOpenOnLlmError() {
        GroundednessJudge judge = judge((system, prompt, tier) -> Flux.error(new IllegalStateException("boom")), true);

        assertThat(judge.judge("q", chunks, "answer").block()).isEmpty();
    }

    @Test
    void disabledJudgeNeverCallsTheLlm() {
        AtomicBoolean called = new AtomicBoolean(false);
        GroundednessJudge judge = judge((system, prompt, tier) -> {
            called.set(true);
            return Flux.just("SUPPORTED");
        }, false);

        assertThat(judge.enabled()).isFalse();
        assertThat(judge.judge("q", chunks, "answer").block()).isEmpty();
        assertThat(called).isFalse();
    }

    private GroundednessJudge judge(LlmClient llm, boolean enabled) {
        RecallProperties props = new RecallProperties(null, null, null, null, null, null, null,
                new RecallProperties.Rag(new RecallProperties.Rag.Judge(enabled, 5)));
        return new GroundednessJudge(llm, meters, props);
    }
}
