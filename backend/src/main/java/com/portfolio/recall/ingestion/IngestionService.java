package com.portfolio.recall.ingestion;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.storage.RawDocumentStore;
import io.micrometer.core.instrument.MeterRegistry;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;

/**
 * Accepts a document and publishes it to Kafka for async processing (docs/adr/0003).
 *
 * <p>The 202 contract (docs/adr/0005): by the time {@link #enqueue} returns, the raw document
 * is archived in MinIO (or, if the archive is down, still travels inline when small enough)
 * AND the event is acknowledged by the broker ({@code acks=all}) — an accepted document
 * cannot be silently lost. Documents above the claim-check threshold are published as an
 * {@code objectKey} reference so Kafka never carries multi-megabyte payloads.
 */
@Service
public class IngestionService {

    private static final Logger log = LoggerFactory.getLogger(IngestionService.class);
    private static final long SEND_TIMEOUT_SECONDS = 10;

    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper json;
    private final RawDocumentStore rawStore;
    private final MeterRegistry meters;
    private final String topic;
    private final int inlineMaxBytes;

    public IngestionService(KafkaTemplate<String, String> kafka, ObjectMapper json,
                            RawDocumentStore rawStore, MeterRegistry meters, RecallProperties props) {
        this.kafka = kafka;
        this.json = json;
        this.rawStore = rawStore;
        this.meters = meters;
        this.topic = props.kafka().ingestionTopic();
        this.inlineMaxBytes = props.storage().inlineMaxBytes();
    }

    public void enqueue(IngestionEvent request) {
        String content = request.content();
        boolean fitsInline = content.getBytes(StandardCharsets.UTF_8).length <= inlineMaxBytes;
        String objectKey = archiveRaw(request, fitsInline);
        IngestionEvent event = new IngestionEvent(
                request.docId(), request.source(), request.lang(),
                fitsInline ? content : null,        // claim check: large payloads travel by reference
                objectKey);
        try {
            kafka.send(topic, event.docId(), json.writeValueAsString(event))
                    .get(SEND_TIMEOUT_SECONDS, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted enqueuing ingestion for " + request.docId(), e);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to enqueue ingestion for " + request.docId(), e);
        }
    }

    /**
     * Archiving is best-effort for small documents (they still travel inline) but mandatory
     * for large ones — without the object there is nothing for the consumer to fetch.
     */
    private String archiveRaw(IngestionEvent request, boolean fitsInline) {
        try {
            return rawStore.store(request.docId(), request.content());
        } catch (RuntimeException e) {
            if (!fitsInline) {
                throw e;
            }
            meters.counter("recall.ingestion.raw.store.failures").increment();
            log.warn("raw archive failed for {} — continuing inline: {}", request.docId(), e.getMessage());
            return null;
        }
    }
}
