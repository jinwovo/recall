package com.portfolio.recall.ingestion;

import jakarta.validation.constraints.NotBlank;

/**
 * Ingestion request body and Kafka payload. Documents above the claim-check threshold travel
 * with {@code content = null} and an {@code objectKey} pointing at the archived raw document
 * in MinIO (docs/adr/0005); small documents carry inline content AND an objectKey (archive).
 *
 * <p>{@code objectKey} is server-assigned — {@link IngestionService} overwrites whatever an
 * API client sends. The {@code @NotBlank} constraints apply only on the HTTP boundary.
 */
public record IngestionEvent(
        @NotBlank String docId,
        String source,
        String lang,
        @NotBlank String content,
        String objectKey) {

    /** API-boundary shape (no objectKey) — kept so existing callers/tests read naturally. */
    public IngestionEvent(String docId, String source, String lang, String content) {
        this(docId, source, lang, content, null);
    }
}
