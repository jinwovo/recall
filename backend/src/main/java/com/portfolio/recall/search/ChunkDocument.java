package com.portfolio.recall.search;

/**
 * The Elasticsearch document for a single chunk. {@code contentHash} doubles as the
 * ES {@code _id} so re-ingestion upserts idempotently (docs/adr/0003).
 */
public record ChunkDocument(
        String docId,
        int chunkIndex,
        String content,
        String source,
        String lang,
        String contentHash,
        float[] embedding) {
}
