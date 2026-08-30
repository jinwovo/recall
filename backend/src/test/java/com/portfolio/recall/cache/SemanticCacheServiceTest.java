package com.portfolio.recall.cache;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Duration;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.ReactiveHashOperations;
import org.springframework.data.redis.core.ReactiveStringRedisTemplate;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * The semantic cache is a single Redis hash scanned app-side on every question, so its
 * failure modes are all about what that scan is allowed to cost — and about staying up
 * when one of the records in it is not what this build expects.
 */
class SemanticCacheServiceTest {

    private static final String KEY = "recall:scache";

    private final ObjectMapper json = new ObjectMapper();
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();

    @SuppressWarnings("unchecked")
    private final ReactiveHashOperations<String, String, String> hash =
            mock(ReactiveHashOperations.class);
    private final ReactiveStringRedisTemplate redis = mock(ReactiveStringRedisTemplate.class);

    private SemanticCacheService cache(double threshold, int maxEntries, long ttlMinutes) {
        when(redis.<String, String>opsForHash()).thenReturn(hash);
        return new SemanticCacheService(redis, json, meters,
                new RecallProperties(null, null, null,
                        new RecallProperties.SemanticCache(threshold, maxEntries, ttlMinutes),
                        null, null, null, null, null));
    }

    /** One stored entry, as it is serialized into the hash. */
    private static String entry(String answer, float... embedding) {
        StringBuilder sb = new StringBuilder("{\"embedding\":[");
        for (int i = 0; i < embedding.length; i++) {
            sb.append(i == 0 ? "" : ",").append(embedding[i]);
        }
        return sb.append("],\"answer\":\"").append(answer).append("\",\"query\":\"q\"}").toString();
    }

    @Test
    void oneUnreadableEntryDoesNotBlindTheWholeCache() {
        // Reactor treats a null from map() as an error, so parsing entries with map() meant a
        // single record this build can no longer read — a schema change, a truncated write —
        // failed every lookup that reached it, silently and from then on.
        SemanticCacheService cache = cache(0.95, 1000, 1440);
        when(hash.values(KEY)).thenReturn(Flux.just(
                "{not json at all",
                entry("cached answer", 1f, 0f)));

        assertThat(cache.lookup(new float[] {1f, 0f}).block()).contains("cached answer");
    }

    @Test
    void theBestMatchAboveTheThresholdWins() {
        SemanticCacheService cache = cache(0.5, 1000, 1440);
        when(hash.values(KEY)).thenReturn(Flux.just(
                entry("weaker match", 1f, 1f),      // cosine 0.707 against [1, 0]
                entry("closest match", 1f, 0f)));   // cosine 1.0

        assertThat(cache.lookup(new float[] {1f, 0f}).block()).contains("closest match");
        assertThat(meters.counter("recall.semantic_cache.hits").count()).isEqualTo(1.0);
    }

    @Test
    void nothingSimilarEnoughIsAMissNotAWrongAnswer() {
        SemanticCacheService cache = cache(0.95, 1000, 1440);
        when(hash.values(KEY)).thenReturn(Flux.just(entry("unrelated", 0f, 1f)));

        assertThat(cache.lookup(new float[] {1f, 0f}).block()).isEqualTo(Optional.empty());
    }

    @Test
    void aFullCacheRefusesNewAnswersRatherThanSlowingEveryLookup() {
        SemanticCacheService cache = cache(0.95, 2, 1440);
        when(hash.size(KEY)).thenReturn(Mono.just(2L));

        cache.put(new float[] {1f, 0f}, "an answer", "q").block();

        verify(hash, never()).put(eq(KEY), anyString(), anyString());
        assertThat(meters.counter("recall.semantic_cache.rejected").count()).isEqualTo(1.0);
    }

    @Test
    void theEntryThatCreatesTheHashArmsTheTtl() {
        SemanticCacheService cache = cache(0.95, 1000, 30);
        when(hash.size(KEY)).thenReturn(Mono.just(0L));
        when(hash.put(eq(KEY), anyString(), anyString())).thenReturn(Mono.just(true));
        when(redis.expire(eq(KEY), any(Duration.class))).thenReturn(Mono.just(true));

        cache.put(new float[] {1f, 0f}, "an answer", "q").block();

        verify(redis).expire(KEY, Duration.ofMinutes(30));
    }

    @Test
    void laterEntriesDoNotPushTheExpiryBack() {
        // A refreshed TTL is a sliding window: under steady traffic it never fires, which is
        // exactly when replaying an answer grounded in a re-ingested corpus is worst.
        SemanticCacheService cache = cache(0.95, 1000, 30);
        when(hash.size(KEY)).thenReturn(Mono.just(7L));
        when(hash.put(eq(KEY), anyString(), anyString())).thenReturn(Mono.just(true));

        cache.put(new float[] {1f, 0f}, "an answer", "q").block();

        verify(hash).put(eq(KEY), anyString(), anyString());
        verify(redis, never()).expire(eq(KEY), any(Duration.class));
    }

    @Test
    void aRedisFailureIsACacheMissNotABrokenAnswer() {
        SemanticCacheService cache = cache(0.95, 1000, 1440);
        when(hash.values(KEY)).thenReturn(Flux.error(new IllegalStateException("redis down")));

        assertThat(cache.lookup(new float[] {1f, 0f}).block()).isEqualTo(Optional.empty());
    }
}
