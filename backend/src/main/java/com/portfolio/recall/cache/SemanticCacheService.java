package com.portfolio.recall.cache;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.common.VectorMath;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveStringRedisTemplate;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;

/**
 * Semantic cache (docs/adr/0002): embed the query, cosine-match against cached answers,
 * and skip the LLM on a near-duplicate question.
 *
 * <p>Scaffold implementation scans a single Redis hash. For production scale, back this with
 * RediSearch / a vector index instead of an app-side linear scan.
 */
@Service
public class SemanticCacheService {

    private static final Logger log = LoggerFactory.getLogger(SemanticCacheService.class);
    private static final String KEY = "recall:scache";

    private final ReactiveStringRedisTemplate redis;
    private final ObjectMapper json;
    private final MeterRegistry meters;
    private final double threshold;

    public SemanticCacheService(ReactiveStringRedisTemplate redis, ObjectMapper json,
                                MeterRegistry meters, RecallProperties props) {
        this.redis = redis;
        this.json = json;
        this.meters = meters;
        this.threshold = props.semanticCache().threshold();
    }

    public Mono<Optional<String>> lookup(float[] queryEmbedding) {
        return redis.<String, String>opsForHash().values(KEY)
                .map(this::parse)
                .filter(e -> e != null && e.embedding() != null)
                .map(e -> new Scored(VectorMath.cosine(queryEmbedding, e.embedding()), e.answer()))
                .filter(s -> s.score() >= threshold)
                .sort((a, b) -> Double.compare(b.score(), a.score()))
                .next()
                .map(s -> {
                    meters.counter("recall.semantic_cache.hits").increment();
                    return Optional.of(s.answer());
                })
                .defaultIfEmpty(Optional.empty())
                .doOnError(e -> log.warn("semantic cache lookup failed: {}", e.getMessage()))
                .onErrorReturn(Optional.empty());
    }

    public Mono<Void> put(float[] queryEmbedding, String answer, String query) {
        CacheEntry entry = new CacheEntry(queryEmbedding, answer, query);
        String value = write(entry);
        if (value == null) {
            return Mono.empty();
        }
        return redis.<String, String>opsForHash()
                .put(KEY, UUID.randomUUID().toString(), value)
                .then()
                .onErrorResume(e -> {
                    log.warn("semantic cache put failed: {}", e.getMessage());
                    return Mono.empty();
                });
    }

    private CacheEntry parse(String s) {
        try {
            return json.readValue(s, CacheEntry.class);
        } catch (Exception e) {
            return null;
        }
    }

    private String write(CacheEntry e) {
        try {
            return json.writeValueAsString(e);
        } catch (Exception ex) {
            return null;
        }
    }

    private record CacheEntry(float[] embedding, String answer, String query) {}

    private record Scored(double score, String answer) {}
}
