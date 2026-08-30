package com.portfolio.recall.cache;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.common.VectorMath;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
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
 * <p>Deliberately still an app-side linear scan over a single Redis hash — for production
 * scale, back this with RediSearch / a vector index instead. What the scan is <em>not</em>
 * allowed to be is unbounded, because the cost of the design falls on the read path: every
 * lookup pulls the entire hash, 1024-dim embeddings and all, across the wire and scores it.
 * So the hash carries two bounds:
 *
 * <ul>
 *   <li><strong>{@code max-entries}</strong> — a cap on the scan itself. Past it, new answers
 *       are refused and counted rather than silently making every future question slower.</li>
 *   <li><strong>{@code ttl-minutes}</strong> — armed once, when an entry first creates the
 *       hash, and never refreshed. A sliding window would never expire under steady traffic,
 *       which is exactly when serving an answer grounded in a since-reindexed corpus is worst.
 *       The whole cache rolls over instead; that is the price of one hash rather than one key
 *       per entry.</li>
 * </ul>
 */
@Service
public class SemanticCacheService {

    private static final Logger log = LoggerFactory.getLogger(SemanticCacheService.class);
    private static final String KEY = "recall:scache";

    private final ReactiveStringRedisTemplate redis;
    private final ObjectMapper json;
    private final MeterRegistry meters;
    private final double threshold;
    private final int maxEntries;
    private final Duration ttl;

    public SemanticCacheService(ReactiveStringRedisTemplate redis, ObjectMapper json,
                                MeterRegistry meters, RecallProperties props) {
        this.redis = redis;
        this.json = json;
        this.meters = meters;
        this.threshold = props.semanticCache().threshold();
        this.maxEntries = props.semanticCache().maxEntries();
        this.ttl = Duration.ofMinutes(props.semanticCache().ttlMinutes());
    }

    public Mono<Optional<String>> lookup(float[] queryEmbedding) {
        return redis.<String, String>opsForHash().values(KEY)
                // mapNotNull, not map: Reactor treats a null from map() as an error, so a
                // single entry this build can no longer parse would take down every lookup
                // after it — the cache failing closed and permanently, on one bad record.
                .mapNotNull(this::parse)
                .filter(e -> e.embedding() != null)
                .map(e -> new Scored(VectorMath.cosine(queryEmbedding, e.embedding()), e.answer()))
                .filter(s -> s.score() >= threshold)
                // reduce, not sort().next(): only the best match is ever read, and sorting
                // buffers every match to find it.
                .reduce((a, b) -> a.score() >= b.score() ? a : b)
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
        return redis.<String, String>opsForHash().size(KEY)
                .flatMap(size -> store(size, value))
                .then()
                .onErrorResume(e -> {
                    log.warn("semantic cache put failed: {}", e.getMessage());
                    return Mono.empty();
                });
    }

    /**
     * Writes one entry if the cache has room, arming the TTL on the entry that creates the
     * hash. Two writers can both observe an empty hash and both arm it — they arm the same
     * duration, so the race is harmless.
     */
    private Mono<Boolean> store(long size, String value) {
        if (size >= maxEntries) {
            meters.counter("recall.semantic_cache.rejected").increment();
            return Mono.empty();
        }
        Mono<Boolean> stored = redis.<String, String>opsForHash()
                .put(KEY, UUID.randomUUID().toString(), value);
        return size == 0 ? stored.then(redis.expire(KEY, ttl)) : stored;
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
