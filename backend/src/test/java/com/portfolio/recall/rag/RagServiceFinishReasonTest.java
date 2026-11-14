package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.cache.SemanticCacheService;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.FinishReason;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.llm.StreamChunk;
import com.portfolio.recall.persistence.QueryLogService;
import com.portfolio.recall.search.RetrievedChunk;
import com.portfolio.recall.search.SearchService;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * An answer cut at {@code max_tokens} reads exactly like a finished one up to the word where it
 * stops, and the semantic cache replays whatever it is given to every paraphrase of the
 * question. So the cache has to be told why generation ended, and so does the reader.
 */
class RagServiceFinishReasonTest {

    private static final RetrievedChunk CHUNK =
            new RetrievedChunk("c1", "d1", 0, "some grounded passage", "src", "en", 0.9);

    private final LlmClient llm = mock(LlmClient.class);
    private final SemanticCacheService cache = mock(SemanticCacheService.class);
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();

    private RagService service() {
        SearchService search = mock(SearchService.class);
        EmbeddingClient embeddings = mock(EmbeddingClient.class);
        SufficiencyCheck sufficiency = mock(SufficiencyCheck.class);
        ConformalSetSizer sizer = mock(ConformalSetSizer.class);
        QueryLogService queryLog = mock(QueryLogService.class);
        GroundednessJudge judge = mock(GroundednessJudge.class);

        when(embeddings.embedOne(anyString())).thenReturn(Mono.just(new float[] {1f, 0f}));
        when(cache.lookup(any())).thenReturn(Mono.just(Optional.empty()));
        when(cache.put(any(), anyString(), anyString())).thenReturn(Mono.empty());
        when(search.hybridWithVector(anyString(), any())).thenReturn(Mono.just(List.of(CHUNK)));
        when(sufficiency.allowGeneration(anyString(), any())).thenReturn(Mono.just(true));
        when(sizer.size(any())).thenReturn(1);
        when(judge.enabled()).thenReturn(false);
        when(queryLog.record(anyString(), anyString(), anyBoolean(), anyInt(), anyInt(),
                anyLong(), any())).thenReturn(Mono.empty());

        RecallProperties props = new RecallProperties(null, null, null, null, null, null, null,
                new RecallProperties.Rag(
                        new RecallProperties.Rag.Judge(false, 5),
                        new RecallProperties.Rag.Sufficiency(false, 0.35, 5),
                        new RecallProperties.Rag.Conformal(false, 0.10, -1.0, 1.0, 12,
                                false, 0.05, 0),
                        new RecallProperties.Rag.Generation(30, 30)),
                null);

        return new RagService(search, embeddings, cache, llm, judge, sufficiency, sizer,
                mock(CoverageMonitor.class), queryLog, new ObjectMapper(), meters, props);
    }

    private void provider(FinishReason reason) {
        when(llm.stream(anyString(), anyString(), any(ModelTier.class))).thenReturn(Flux.just(
                new StreamChunk.Text("The answer is"),
                new StreamChunk.Text(" forty"),
                new StreamChunk.End(reason)));
    }

    private List<ServerSentEvent<String>> drain() {
        return service().ask("q").collectList().block(Duration.ofSeconds(10));
    }

    private static String dataOf(List<ServerSentEvent<String>> events, String name) {
        return events.stream().filter(e -> name.equals(e.event())).map(ServerSentEvent::data)
                .findFirst().orElseThrow();
    }

    @Test
    void aCompleteAnswerIsCachedAndSaysSo() {
        provider(FinishReason.COMPLETE);

        List<ServerSentEvent<String>> events = drain();

        assertThat(events).extracting(ServerSentEvent::event)
                .containsSubsequence("token", "token", "finish", "done");
        assertThat(dataOf(events, "finish")).isEqualTo("{\"reason\":\"COMPLETE\"}");
        verify(cache).put(any(), anyString(), anyString());
    }

    @Test
    void aTruncatedAnswerIsShownButNeverCached() {
        provider(FinishReason.TRUNCATED);

        List<ServerSentEvent<String>> events = drain();

        // Still streamed and still closed normally — the tokens are real, just not all of them.
        assertThat(events).extracting(ServerSentEvent::event).endsWith("done").doesNotContain("error");
        assertThat(dataOf(events, "finish")).isEqualTo("{\"reason\":\"TRUNCATED\"}");
        verify(cache, never()).put(any(), anyString(), anyString());
        assertThat(meters.counter("recall.rag.generation.finish", "reason", "truncated").count())
                .isEqualTo(1.0);
    }

    @Test
    void aRefusedAnswerIsNeverCached() {
        provider(FinishReason.REFUSED);

        drain();

        verify(cache, never()).put(any(), anyString(), anyString());
    }

    @Test
    void aProviderThatDoesNotReportKeepsThePreviousBehaviour() {
        // Before the reason was read at all, every answer was treated as complete. A provider
        // that still cannot say must not silently lose caching.
        provider(FinishReason.UNREPORTED);

        drain();

        verify(cache).put(any(), anyString(), anyString());
    }
}
