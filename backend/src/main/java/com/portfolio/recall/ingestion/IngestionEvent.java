package com.portfolio.recall.ingestion;

/**
 * Ingestion request body and Kafka payload. For large documents, store the raw bytes in MinIO
 * and pass an objectKey instead of inline content (see docs/adr/0003).
 */
public record IngestionEvent(String docId, String source, String lang, String content) {}
