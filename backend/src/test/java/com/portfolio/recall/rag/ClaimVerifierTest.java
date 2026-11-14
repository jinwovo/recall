package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.startsWith;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.embedding.EmbeddingClient.RerankItem;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;

class ClaimVerifierTest {

    private static final List<RetrievedChunk> CHUNKS = List.of(
            new RetrievedChunk("c1", "d1", 0, "passage one", "src", "en", 0.9),
            new RetrievedChunk("c2", "d2", 0, "passage two", "src", "en", 0.8));
    private static final String ANSWER = "Supported claim here [1]. Invented claim here.";

    private final EmbeddingClient embeddings = mock(EmbeddingClient.class);
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();

    private ClaimVerifier verifier(boolean enabled, double threshold, int maxClaims, int timeout) {
        RecallProperties props = new RecallProperties(null, null, null, null, null, null, null,
                new RecallProperties.Rag(null, null, null, null,
                        new RecallProperties.Rag.Factuality(enabled, 0.10, threshold, maxClaims, timeout)),
                null);
        return new ClaimVerifier(embeddings, meters, props);
    }

    private void scores() {
        // The claim's score is its best passage, whichever that is.
        when(embeddings.rerank(startsWith("Supported"), anyList(), anyInt()))
                .thenReturn(Mono.just(List.of(new RerankItem(1, 0.92), new RerankItem(0, 0.40))));
        when(embeddings.rerank(startsWith("Invented"), anyList(), anyInt()))
                .thenReturn(Mono.just(List.of(new RerankItem(0, 0.05), new RerankItem(1, 0.03))));
    }

    private ClaimReport run(ClaimVerifier v) {
        return v.verify(CHUNKS, ANSWER).block(Duration.ofSeconds(10)).orElseThrow();
    }

    @Test
    void aCalibratedThresholdWithholdsTheClaimNoPassageSupports() {
        scores();
        ClaimReport r = run(verifier(true, 0.5, 12, 10));

        assertThat(r.claims()).extracting(ClaimReport.Claim::score).containsExactly(0.92, 0.05);
        assertThat(r.claims()).extracting(ClaimReport.Claim::kept).containsExactly(true, false);
        assertThat(r.certified()).isTrue();
        assertThat(r.withheld()).isEqualTo(1);
        assertThat(meters.counter("recall.rag.claims", "outcome", "withheld").count()).isEqualTo(1.0);
    }

    @Test
    void aClaimExactlyAtTheThresholdIsWithheldAsInTheCalibration() {
        scores();
        assertThat(run(verifier(true, 0.05, 12, 10)).claims().get(1).kept()).isFalse();
    }

    @Test
    void uncalibratedIsObserveModeScoresReportedNothingWithheld() {
        scores();
        ClaimReport r = run(verifier(true, -1, 12, 10));

        assertThat(r.threshold()).isNull();
        assertThat(r.certified()).isFalse();
        assertThat(r.claims()).allMatch(ClaimReport.Claim::kept);
        assertThat(r.claims()).extracting(ClaimReport.Claim::score).containsExactly(0.92, 0.05);
    }

    @Test
    void claimsPastTheCapAreUnscoredKeptAndVoidTheCertificate() {
        scores();
        ClaimReport r = run(verifier(true, 0.5, 1, 10));

        assertThat(r.claims().get(1).score()).isNull();
        assertThat(r.claims().get(1).kept()).isTrue();
        assertThat(r.certified()).isFalse();
        verify(embeddings, times(1)).rerank(anyString(), anyList(), anyInt());
    }

    @Test
    void aSidecarThatHangsFailsOpen() {
        when(embeddings.rerank(anyString(), anyList(), anyInt())).thenReturn(Mono.never());

        Optional<ClaimReport> r = verifier(true, 0.5, 12, 1).verify(CHUNKS, ANSWER)
                .block(Duration.ofSeconds(10));

        assertThat(r).isEmpty();
        assertThat(meters.counter("recall.rag.claims.skipped").count()).isEqualTo(1.0);
    }

    @Test
    void disabledNeverCallsTheSidecar() {
        assertThat(verifier(false, 0.5, 12, 10).verify(CHUNKS, ANSWER).block()).isEmpty();
        verify(embeddings, never()).rerank(anyString(), anyList(), anyInt());
    }
}
