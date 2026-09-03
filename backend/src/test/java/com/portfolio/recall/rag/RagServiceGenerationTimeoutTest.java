package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.cache.SemanticCacheService;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.persistence.QueryLogService;
import com.portfolio.recall.search.RetrievedChunk;
import com.portfolio.recall.search.SearchService;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * Generation is the one path in {@link RagService} that cannot fail open — there is no
 * answer to fall back to — so it is the one that has to fail <em>closed</em> on a provider
 * that stops sending. Unbounded, the SSE connection stays open for as long as the provider
 * hangs, and the tail that judges, caches and logs the answer never runs because the stream
 * never completes.
 *
 * <p>What is bounded is the gap between tokens, not the answer: a long answer that keeps
 * arriving is healthy, and the third test is the one that would fail if someone replaced
 * this with a deadline. The bounds are injected at one and two seconds rather than the
 * shipped 30/120 so the suite stays fast; the shipped values are a tuning question, the
 * behaviour here is the contract.
 */
class RagServiceGenerationTimeoutTest {

    private static final int FIRST_TOKEN_SECONDS = 2;
    private static final int STALL_SECONDS = 1;
    private static final Duration PATIENCE = Duration.ofSeconds(15);

    private final LlmClient llm = mock(LlmClient.class);

    /** One chunk, so retrieval neither abstains nor short-circuits to the canned IDK. */
    private static final RetrievedChunk CHUNK =
            new RetrievedChunk("c1", "d1", 0, "some grounded passage", "src", "en", 0.9);

    private RagService service() {
        SearchService search = mock(SearchService.class);
        EmbeddingClient embeddings = mock(EmbeddingClient.class);
        SemanticCacheService cache = mock(SemanticCacheService.class);
        SufficiencyCheck sufficiency = mock(SufficiencyCheck.class);
        ConformalSetSizer sizer = mock(ConformalSetSizer.class);
        QueryLogService queryLog = mock(QueryLogService.class);

        when(embeddings.embedOne(anyString())).thenReturn(Mono.just(new float[] {1f, 0f}));
        when(cache.lookup(any())).thenReturn(Mono.just(Optional.empty()));
        // Only the uninterrupted stream reaches the tail; the timeouts short-circuit past it.
        when(cache.put(any(), anyString(), anyString())).thenReturn(Mono.empty());
        when(search.hybridWithVector(anyString(), any())).thenReturn(Mono.just(List.of(CHUNK)));
        when(sufficiency.allowGeneration(anyString(), any())).thenReturn(Mono.just(true));
        when(sizer.size(any())).thenReturn(1);
        when(queryLog.record(anyString(), anyString(), anyBoolean(), anyInt(), anyInt(),
                anyLong(), any())).thenReturn(Mono.empty());

        RecallProperties props = new RecallProperties(null, null, null, null, null, null, null,
                new RecallProperties.Rag(
                        new RecallProperties.Rag.Judge(false, 5),
                        new RecallProperties.Rag.Sufficiency(false, 0.35, 5),
                        new RecallProperties.Rag.Conformal(false, 0.10, -1.0, 1.0, 12,
                                false, 0.05, 0),
                        new RecallProperties.Rag.Generation(FIRST_TOKEN_SECONDS, STALL_SECONDS)),
                null);

        return new RagService(search, embeddings, cache, llm,
                mock(GroundednessJudge.class), sufficiency, sizer,
                mock(CoverageMonitor.class), queryLog, new ObjectMapper(), props);
    }

    /** Drains the SSE stream, returning the event names in order. */
    private List<String> eventsOf(Flux<ServerSentEvent<String>> stream) {
        return stream.map(ServerSentEvent::event).collectList().block(PATIENCE);
    }

    @Test
    void aProviderThatNeverSendsAnythingIsCutOffRatherThanHeldOpen() {
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.never());

        long start = System.nanoTime();
        List<String> events = eventsOf(service().ask("q"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000;

        assertThat(events).endsWith("error");
        assertThat(events).doesNotContain("done");
        assertThat(elapsedMs).isBetween((long) FIRST_TOKEN_SECONDS * 1000 - 200, PATIENCE.toMillis());
    }

    @Test
    void aStallMidAnswerIsCutAtTheShorterBoundNotTheFirstTokenOne() {
        // The first-token budget is already spent, so the gap that matters now is the stall
        // bound. Sharing one timeout would leave this hanging for the longer prefill budget.
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.concat(Flux.just("partial"), Flux.never()));

        long start = System.nanoTime();
        List<String> events = eventsOf(service().ask("q"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000;

        assertThat(events).contains("token").endsWith("error");
        assertThat(elapsedMs).isLessThan((long) FIRST_TOKEN_SECONDS * 1000);
    }

    @Test
    void aSlowButSteadyAnswerIsNotCutOff() {
        // Total elapsed exceeds the stall bound several times over; no single gap does. A
        // deadline on the answer would kill this, which is the reason the bound is on the gap.
        when(llm.streamAnswer(anyString(), anyString(), any(ModelTier.class)))
                .thenReturn(Flux.interval(Duration.ofMillis(300)).take(8).map(i -> "tok" + i));

        List<String> events = eventsOf(service().ask("q"));

        assertThat(events).doesNotContain("error");
        assertThat(events.stream().filter("token"::equals).count()).isEqualTo(8);
    }
}
