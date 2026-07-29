package com.portfolio.recall.admin;

import java.time.Instant;
import java.util.List;

/**
 * Read-only view of the ingestion dead-letter topic (docs/adr/0006).
 *
 * <p>{@code depth} counts records the replay group has not consumed yet — the operational
 * backlog. {@code records} is a bounded peek from the head of the topic; already-replayed
 * records stay visible (with {@code pending=false}) until Kafka retention expires them,
 * preserving the forensic trail.
 */
public record DlqSnapshot(long depth, List<DlqRecord> records) {

    /** One dead-lettered record with the forensic headers decoded (docs/adr/0005). */
    public record DlqRecord(
            String key,
            int partition,
            long offset,
            Instant timestamp,
            boolean pending,
            String originalTopic,
            Long originalOffset,
            String exceptionType,
            String exceptionMessage,
            int replays,
            String payloadPreview) {}
}
