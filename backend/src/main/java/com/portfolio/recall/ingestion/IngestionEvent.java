package com.portfolio.recall.ingestion;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * Ingestion request body and Kafka payload. Documents above the claim-check threshold travel
 * with {@code content = null} and an {@code objectKey} pointing at the archived raw document
 * in MinIO (docs/adr/0005); small documents carry inline content AND an objectKey (archive).
 *
 * <p>{@code objectKey} is server-assigned — {@link IngestionService} overwrites whatever an
 * API client sends. The constraints here apply only on the HTTP boundary.
 *
 * <p>{@code docId} is constrained because it is not just an identifier: it is concatenated
 * into the archive key as {@code docs/<docId>/raw.txt}. A docId carrying a separator writes
 * outside the one-prefix-per-document layout and can land two different documents on the
 * same key, where the second silently overwrites the first. The rule is deliberately a
 * denylist rather than an allowlist — {@code scripts/ingest_folder.py} slugs paths into
 * Hangul-containing ids, and those are fine; separators, {@code ..} and control characters
 * are not.
 */
public record IngestionEvent(
        @NotBlank
        @Size(max = 200, message = "docId must be at most 200 characters")
        @Pattern(regexp = "^(?!.*\\.\\.)[^/\\\\\\p{Cntrl}]+$",
                message = "docId must not contain '/', '\\', '..' or control characters")
        String docId,
        String source,
        String lang,
        @NotBlank String content,
        String objectKey) {

    /** API-boundary shape (no objectKey) — kept so existing callers/tests read naturally. */
    public IngestionEvent(String docId, String source, String lang, String content) {
        this(docId, source, lang, content, null);
    }
}
