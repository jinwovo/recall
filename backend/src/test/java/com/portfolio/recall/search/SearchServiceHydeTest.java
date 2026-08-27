package com.portfolio.recall.search;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * HyDE mode (docs/adr/0008): what gets embedded is the LLM's hypothetical passage, not the
 * question — and when there is no usable generation, the mode degrades to plain vector
 * search over the original query instead of failing.
 */
class SearchServiceHydeTest {

    private static final float[] VEC = {1f, 0f, 0f, 0f};

    private final DocumentIndex index = mock(DocumentIndex.class);
    private final EmbeddingClient embeddings = mock(EmbeddingClient.class);
    private final LlmClient llm = mock(LlmClient.class);
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();
    private SearchService service;

    @BeforeEach
    void setUp() {
        RecallProperties props = new RecallProperties(
                new RecallProperties.Elasticsearch("http://localhost:9200", "recall-docs"),
                new RecallProperties.Embedding("http://localhost:8000", 4),
                new RecallProperties.Kafka("t", "t.dlq", 3, 100, 1),
                new RecallProperties.SemanticCache(0.95),
                new RecallProperties.Retrieval(50, 8, 60),
                new RecallProperties.Models("primary", "balanced", "cheap"),
                new RecallProperties.Llm("ollama", null, null),
                new RecallProperties.Rag(new RecallProperties.Rag.Judge(false, 5),
                        new RecallProperties.Rag.Sufficiency(false, 0.35, 5),
                        new RecallProperties.Rag.Conformal(false, 0.10, -1.0, 1.0, 12)),
                new RecallProperties.Storage("http://localhost:9000", "a", "s", "b", 65536));
        service = new SearchService(index, embeddings, llm, props, meters);
    }

    @Test
    void embedsTheHypotheticalPassageNotTheQuestion() {
        when(llm.streamAnswer(anyString(), anyString(), eq(ModelTier.CHEAP)))
                .thenReturn(Flux.just("Kubernetes restarts ", "OOMKilled containers."));
        when(embeddings.embedOne("Kubernetes restarts OOMKilled containers."))
                .thenReturn(Mono.just(VEC));
        RetrievedChunk hit = chunk("k8s-oomkilled");
        when(index.knn(any(float[].class), anyInt())).thenReturn(List.of(hit));

        List<RetrievedChunk> results =
                service.search("왜 컨테이너가 죽었어?", SearchMode.HYDE).block();

        assertThat(results).containsExactly(hit);
        verify(embeddings).embedOne("Kubernetes restarts OOMKilled containers.");
        assertThat(meters.counter("recall.search.hyde.fallbacks").count()).isZero();
    }

    @Test
    void llmFailureFallsBackToVectorSearchOverTheQuery() {
        when(llm.streamAnswer(anyString(), anyString(), eq(ModelTier.CHEAP)))
                .thenReturn(Flux.error(new IllegalStateException("no provider (simulated)")));
        when(embeddings.embedOne("why did my container die")).thenReturn(Mono.just(VEC));
        when(index.knn(any(float[].class), anyInt())).thenReturn(List.of(chunk("k8s-oomkilled")));

        List<RetrievedChunk> results =
                service.search("why did my container die", SearchMode.HYDE).block();

        assertThat(results).hasSize(1);
        verify(embeddings).embedOne("why did my container die");   // the query, not a passage
        assertThat(meters.counter("recall.search.hyde.fallbacks").count()).isEqualTo(1.0);
    }

    @Test
    void blankGenerationAlsoFallsBack() {
        when(llm.streamAnswer(anyString(), anyString(), eq(ModelTier.CHEAP)))
                .thenReturn(Flux.just("   ", "\n"));
        when(embeddings.embedOne("q")).thenReturn(Mono.just(VEC));
        when(index.knn(any(float[].class), anyInt())).thenReturn(List.of());

        service.search("q", SearchMode.HYDE).block();

        verify(embeddings).embedOne("q");
        assertThat(meters.counter("recall.search.hyde.fallbacks").count()).isEqualTo(1.0);
    }

    private static RetrievedChunk chunk(String docId) {
        return new RetrievedChunk(docId + "#0", docId, 0, "content", "src", "en", 1.0);
    }
}
