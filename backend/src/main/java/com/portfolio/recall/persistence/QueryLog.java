package com.portfolio.recall.persistence;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** Per-question ledger row — feeds latency / cache-hit / cost analysis. */
@Entity
@Table(name = "query_log")
public class QueryLog {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(length = 2000)
    private String query;

    private String mode;
    private boolean cacheHit;
    private int sourceCount;
    private int answerChars;
    private long latencyMs;

    /** Post-hoc judge score 1.0/0.5/0.0 (docs/adr/0004); null = not judged (cache hit, abstention, judge off/failed). */
    private Double groundedness;

    private Instant createdAt = Instant.now();

    protected QueryLog() {}

    public QueryLog(String query, String mode, boolean cacheHit, int sourceCount,
                    int answerChars, long latencyMs, Double groundedness) {
        this.query = query;
        this.mode = mode;
        this.cacheHit = cacheHit;
        this.sourceCount = sourceCount;
        this.answerChars = answerChars;
        this.latencyMs = latencyMs;
        this.groundedness = groundedness;
    }

    public Long getId() {
        return id;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
