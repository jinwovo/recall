package com.portfolio.recall.ingestion;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.testcontainers.elasticsearch.ElasticsearchContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Validates ADR 0003: re-ingesting the same content must NOT create duplicate chunks.
 * Upsert keyed by contentHash → N identical upserts converge to a single ES document.
 *
 * Disabled by default (needs Docker). Remove @Disabled to run locally / in CI with services.
 */
@Disabled("Integration test — requires Docker (Testcontainers). Remove @Disabled to run.")
@Testcontainers
class IngestionIdempotencyIT {

    @Container
    static final ElasticsearchContainer ES =
            new ElasticsearchContainer("docker.elastic.co/elasticsearch/elasticsearch:8.15.0")
                    .withEnv("xpack.security.enabled", "false");

    @Test
    void duplicateUpsertProducesSingleDocument() {
        // TODO:
        //  1. Build ElasticsearchDocumentIndex against ES.getHttpHostAddress().
        //  2. ensureIndex(); upsert the same ChunkDocument (same contentHash) N times concurrently.
        //  3. Refresh + count → assert exactly 1 document for that hash (no dup/loss).
    }
}
