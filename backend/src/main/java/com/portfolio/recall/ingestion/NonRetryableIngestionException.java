package com.portfolio.recall.ingestion;

/**
 * Marks an ingestion failure as a poison pill: redelivery can never succeed (malformed JSON,
 * missing required fields), so the error handler skips the backoff retries and dead-letters
 * the record immediately (docs/adr/0005 — see KafkaErrorHandlingConfig).
 */
public class NonRetryableIngestionException extends RuntimeException {

    public NonRetryableIngestionException(String message) {
        super(message);
    }

    public NonRetryableIngestionException(String message, Throwable cause) {
        super(message, cause);
    }
}
