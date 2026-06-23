package com.portfolio.recall.search;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
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
 * embed query → BM25 + kNN (candidates) → RRF fuse → cross-encoder rerank.
 *
 * <p>Each mode returns a ranked list up to {@code candidates}; the eval harness slices [:K].
 * RAG calls {@link #hybridWithVector} with a precomputed query embedding (no double-embed).
 */
@Service
public class SearchService {

    private static final Logger log = LoggerFactory.getLogger(SearchService.class);

    private final DocumentIndex index;
    private final EmbeddingClient embeddings;
    private final RecallProperties props;
    private final MeterRegistry meters;

    public SearchService(DocumentIndex index, EmbeddingClient embeddings,
                         RecallProperties props, MeterRegistry meters) {
        this.index = index;
        this.embeddings = embeddings;
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

    /** Default search is hybrid. */
    public Mono<List<RetrievedChunk>> search(String query) {
        return search(query, SearchMode.HYBRID);
    }

    public Mono<List<RetrievedChunk>> search(String query, SearchMode mode) {
        int candidates = props.retrieval().candidates();
        return switch (mode) {
            case BM25 -> timed(mode, blocking(() -> index.bm25(query, candidates)));
            case VECTOR -> timed(mode, embeddings.embedOne(query)
                    .flatMap(vec -> blocking(() -> index.knn(vec, candidates))));
            case HYBRID -> embeddings.embedOne(query).flatMap(vec -> hybridWithVector(query, vec));
        };
    }

    /** Hybrid retrieval with a precomputed query embedding (reused from the RAG cache key). */
    public Mono<List<RetrievedChunk>> hybridWithVector(String query, float[] vector) {
        int candidates = props.retrieval().candidates();
        int rrfK = props.retrieval().rrfK();
        Mono<List<RetrievedChunk>> fused = blocking(() -> {
            List<RetrievedChunk> bm25 = index.bm25(query, candidates);
            List<RetrievedChunk> knn = index.knn(vector, candidates);
            return ReciprocalRankFusion.fuse(bm25, knn, rrfK, candidates);
        });
        return timed(SearchMode.HYBRID, fused.flatMap(f -> rerankAll(query, f)));
    }

    private Mono<List<RetrievedChunk>> rerankAll(String query, List<RetrievedChunk> fused) {
        if (fused.isEmpty()) {
            return Mono.just(List.of());
        }
        List<String> passages = fused.stream().map(RetrievedChunk::content).toList();
        // Rerank the whole fused set so eval gets a fully ordered list; RAG slices topK after.
        return embeddings.rerank(query, passages, passages.size())
                .map(items -> items.stream()
                        .map(it -> fused.get(it.index()).withScore(it.score()))
                        .toList());
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
