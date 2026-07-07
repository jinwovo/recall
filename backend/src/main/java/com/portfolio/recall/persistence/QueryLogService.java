package com.portfolio.recall.persistence;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * Persists {@link QueryLog} rows off the reactive request path (blocking JPA on boundedElastic).
 * Logging failures never break the response.
 */
@Service
public class QueryLogService {

    private static final Logger log = LoggerFactory.getLogger(QueryLogService.class);

    private final QueryLogRepository repo;

    public QueryLogService(QueryLogRepository repo) {
        this.repo = repo;
    }

    public Mono<Void> record(String query, String mode, boolean cacheHit,
                             int sourceCount, int answerChars, long latencyMs, Double groundedness) {
        return Mono.fromRunnable(() ->
                        repo.save(new QueryLog(query, mode, cacheHit, sourceCount, answerChars, latencyMs, groundedness)))
                .subscribeOn(Schedulers.boundedElastic())
                .then()
                .onErrorResume(e -> {
                    log.warn("query log persist failed: {}", e.getMessage());
                    return Mono.empty();
                });
    }
}
