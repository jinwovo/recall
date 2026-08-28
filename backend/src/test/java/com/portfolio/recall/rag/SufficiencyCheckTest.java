package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;

/**
 * Pre-generation sufficiency gate (docs/adr/0009): confident retrievals skip the LLM
 * entirely; only a clear INSUFFICIENT verdict blocks generation; everything else —
 * errors, timeouts, unparseable output, disabled flag — fails open.
 */
class SufficiencyCheckTest {

    private final LlmClient llm = mock(LlmClient.class);
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();

    private SufficiencyCheck check(boolean enabled, double threshold) {
        RecallProperties props = new RecallProperties(
                null, null, null, null, null, null, null,
                new RecallProperties.Rag(new RecallProperties.Rag.Judge(false, 5),
                        new RecallProperties.Rag.Sufficiency(enabled, threshold, 5),
                        new RecallProperties.Rag.Conformal(false, 0.10, -1.0, 1.0, 12, false, 0.05, 0)),
                null);
        return new SufficiencyCheck(llm, meters, props);
    }

    private static List<RetrievedChunk> chunks(double topScore) {
        return List.of(new RetrievedChunk("d#0", "d", 0, "content", "src", "en", topScore));
    }

    @Test
    void confidentRetrievalSkipsTheLlm() {
        Boolean allowed = check(true, 0.35).allowGeneration("q", chunks(0.92)).block();

        assertThat(allowed).isTrue();
        verify(llm, never()).streamAnswer(anyString(), anyString(), any());
        assertThat(meters.counter("recall.rag.sufficiency.skips").count()).isEqualTo(1.0);
    }

    @Test
    void lowConfidenceInsufficientVerdictBlocksGeneration() {
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.just("INSUF", "FICIENT"));

        Boolean allowed = check(true, 0.35).allowGeneration("q", chunks(0.08)).block();

        assertThat(allowed).isFalse();
        assertThat(meters.counter("recall.rag.sufficiency.verdicts", "verdict", "insufficient")
                .count()).isEqualTo(1.0);
    }

    @Test
    void lowConfidenceSufficientVerdictAllowsGeneration() {
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.just("SUFFICIENT"));

        assertThat(check(true, 0.35).allowGeneration("q", chunks(0.08)).block()).isTrue();
        assertThat(meters.counter("recall.rag.sufficiency.verdicts", "verdict", "sufficient")
                .count()).isEqualTo(1.0);
    }

    @Test
    void insufficientWinsOverItsSufficientSubstring() {
        assertThat(SufficiencyCheck.parse("Verdict: INSUFFICIENT")).isFalse();
        assertThat(SufficiencyCheck.parse("SUFFICIENT")).isTrue();
        assertThat(SufficiencyCheck.parse("no verdict here")).isTrue();   // fail-open
        assertThat(SufficiencyCheck.parse(null)).isTrue();
    }

    @Test
    void llmErrorFailsOpen() {
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.error(new IllegalStateException("provider down (simulated)")));

        assertThat(check(true, 0.35).allowGeneration("q", chunks(0.08)).block()).isTrue();
    }

    @Test
    void disabledGateNeverCallsTheLlm() {
        assertThat(check(false, 0.35).allowGeneration("q", chunks(0.01)).block()).isTrue();
        verify(llm, never()).streamAnswer(anyString(), anyString(), any());
    }
}
