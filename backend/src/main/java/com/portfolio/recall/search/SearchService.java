package com.portfolio.recall.search;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.Callable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * Hybrid retrieval orchestration (docs/adr/0001):
 * embed query → BM25 + kNN (candidates) → RRF fuse → rerank.
 *
 * <p>The rerank stage is pluggable (docs/adr/0008): the cross-encoder (default) or
 * bge-m3's tri-modal self-hybrid scoring. {@code HYDE} mode retrieves with the embedding
 * of an LLM-generated hypothetical answer instead of the query embedding — fail-open to
 * plain vector search when no LLM is available.
 *
 * <p>Each mode returns a ranked list up to {@code candidates}; the eval harness slices [:K].
 * RAG calls {@link #hybridWithVector} with a precomputed query embedding (no double-embed).
 */
@Service
public class SearchService {

    private static final Logger log = LoggerFactory.getLogger(SearchService.class);

    /** HyDE (docs/adr/0008): the generated passage, not the question, is what gets embedded. */
    private static final String HYDE_SYSTEM =
            "You write short technical documentation passages. Output only the passage text.";
    private static final String HYDE_PROMPT = """
            Write one short, factual documentation paragraph (3-4 sentences, plain text) that \
            would directly answer the question below, as if quoted from official docs. \
            Do not address the reader and do not mention this is hypothetical.

            Question: %s

            Passage:""";
    private static final Duration HYDE_TIMEOUT = Duration.ofSeconds(30);

    private final DocumentIndex index;
    private final EmbeddingClient embeddings;
    private final LlmClient llm;
    private final RecallProperties props;
    private final MeterRegistry meters;

    public SearchService(DocumentIndex index, EmbeddingClient embeddings, LlmClient llm,
                         RecallProperties props, MeterRegistry meters) {
        this.index = index;
        this.embeddings = embeddings;
        this.llm = llm;
        this.props = props;
        this.meters = meters;
    }

    @EventListener(ApplicationReadyEvent.class)
    void initIndex() {
        try {
            index.ensureIndex();
        } catch (RuntimeException e) {
            log.warn("Could not ensure ES index at startup (is Elasticsearch up?): {}", e.getMessage());
        }
    }

    /** Default search is hybrid with the cross-encoder. */
    public Mono<List<RetrievedChunk>> search(String query) {
        return search(query, SearchMode.HYBRID, RerankStrategy.CROSS_ENCODER);
    }

    public Mono<List<RetrievedChunk>> search(String query, SearchMode mode) {
        return search(query, mode, RerankStrategy.CROSS_ENCODER);
    }

    public Mono<List<RetrievedChunk>> search(String query, SearchMode mode, RerankStrategy strategy) {
        return search(query, mode, strategy, TuningOverrides.NONE);
    }

    public Mono<List<RetrievedChunk>> search(String query, SearchMode mode, RerankStrategy strategy,
                                             TuningOverrides tuning) {
        int candidates = tuning.candidatesOr(props.retrieval().candidates());
        return switch (mode) {
            case BM25 -> timed(mode, blocking(() -> index.bm25(query, candidates)));
            case VECTOR -> timed(mode, embeddings.embedOne(query)
                    .flatMap(vec -> blocking(() -> index.knn(vec, candidates))));
            case HYBRID -> embeddings.embedOne(query)
                    .flatMap(vec -> hybridWithVector(query, vec, strategy, tuning));
            case HYDE -> timed(mode, hyde(query, candidates));
        };
    }

    /** Hybrid retrieval with a precomputed query embedding (reused from the RAG cache key). */
    public Mono<List<RetrievedChunk>> hybridWithVector(String query, float[] vector) {
        return hybridWithVector(query, vector, RerankStrategy.CROSS_ENCODER, TuningOverrides.NONE);
    }

    public Mono<List<RetrievedChunk>> hybridWithVector(String query, float[] vector,
                                                       RerankStrategy strategy, TuningOverrides tuning) {
        int candidates = tuning.candidatesOr(props.retrieval().candidates());
        int rrfK = tuning.rrfKOr(props.retrieval().rrfK());
        Mono<List<RetrievedChunk>> fused = blocking(() -> {
            List<RetrievedChunk> bm25 = index.bm25(query, candidates);
            List<RetrievedChunk> knn = index.knn(vector, candidates);
            return ReciprocalRankFusion.fuse(bm25, knn, rrfK, candidates);
        });
        return timed(SearchMode.HYBRID, fused.flatMap(f -> rerankAll(query, f, strategy)));
    }

    private Mono<List<RetrievedChunk>> rerankAll(String query, List<RetrievedChunk> fused,
                                                 RerankStrategy strategy) {
        if (fused.isEmpty()) {
            return Mono.just(List.of());
        }
        List<String> passages = fused.stream().map(RetrievedChunk::content).toList();
        // Rerank the whole fused set so eval gets a fully ordered list; RAG slices topK after.
        var scored = switch (strategy) {
            case CROSS_ENCODER -> embeddings.rerank(query, passages, passages.size());
            case M3 -> embeddings.scoreM3(query, passages);
        };
        return scored.map(items -> items.stream()
                .map(it -> fused.get(it.index()).withScore(it.score()))
                .toList());
    }

    /**
     * HyDE: answer the question hypothetically with the CHEAP-tier model, embed the
     * hypothetical passage, and kNN with that. On any LLM failure (no provider, timeout)
     * the mode degrades to plain vector search over the original query — retrieval keeps
     * working without an LLM, it just loses the HyDE lift.
     */
    private Mono<List<RetrievedChunk>> hyde(String query, int candidates) {
        return llm.streamAnswer(HYDE_SYSTEM, HYDE_PROMPT.formatted(query), ModelTier.CHEAP)
                .collect(StringBuilder::new, StringBuilder::append)
                .map(StringBuilder::toString)
                .map(String::strip)
                .filter(passage -> !passage.isBlank())
                .timeout(HYDE_TIMEOUT)
                .doOnNext(passage -> log.debug("HyDE passage for '{}': {}", query, passage))
                .onErrorResume(e -> {
                    log.warn("HyDE generation failed ({}) — falling back to vector search",
                            e.toString());
                    return Mono.empty();
                })
                .switchIfEmpty(Mono.fromSupplier(() -> {     // error OR blank generation
                    meters.counter("recall.search.hyde.fallbacks").increment();
                    return query;                            // fallback: embed the query itself
                }))
                .flatMap(text -> embeddings.embedOne(text))
                .flatMap(vec -> blocking(() -> index.knn(vec, candidates)));
    }

    private Mono<List<RetrievedChunk>> timed(SearchMode mode, Mono<List<RetrievedChunk>> work) {
        return Mono.defer(() -> {
            long start = System.nanoTime();
            return work.doOnNext(r -> meters
                    .timer("recall.retrieval.latency", "mode", mode.name())
                    .record(Duration.ofNanos(System.nanoTime() - start)));
        });
    }

    private Mono<List<RetrievedChunk>> blocking(Callable<List<RetrievedChunk>> work) {
        return Mono.fromCallable(work).subscribeOn(Schedulers.boundedElastic());
    }
}
