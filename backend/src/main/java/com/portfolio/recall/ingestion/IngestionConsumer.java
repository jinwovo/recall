package com.portfolio.recall.ingestion;

import com.fasterxml.jackson.core.JacksonException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.common.Hashing;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.search.ChunkDocument;
import com.portfolio.recall.search.DocumentIndex;
import com.portfolio.recall.storage.RawDocumentStore;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * Consumes ingestion events and runs the pipeline: resolve content (inline or MinIO claim
 * check) → chunk → embed (batched) → idempotent upsert. Idempotency: each chunk's ES _id is
 * sha256(docId + index + content), so retries/duplicates and concurrent ingestion converge
 * to the same index state (docs/adr/0003).
 *
 * <p>Failures propagate to the container's {@code DefaultErrorHandler} (docs/adr/0005):
 * transient errors (sidecar/ES/MinIO down) retry with backoff, poison pills and exhausted
 * retries land on the dead-letter topic — nothing is silently dropped.
 */
@Component
public class IngestionConsumer {

    private static final Logger log = LoggerFactory.getLogger(IngestionConsumer.class);

    private final Chunker chunker;
    private final EmbeddingClient embeddings;
    private final DocumentIndex index;
    private final RawDocumentStore rawStore;
    private final ObjectMapper json;
    private final MeterRegistry meters;

    public IngestionConsumer(Chunker chunker, EmbeddingClient embeddings, DocumentIndex index,
                             RawDocumentStore rawStore, ObjectMapper json, MeterRegistry meters) {
        this.chunker = chunker;
        this.embeddings = embeddings;
        this.index = index;
        this.rawStore = rawStore;
        this.json = json;
        this.meters = meters;
    }

    /**
     * One listener thread per configured concurrency, capped in practice by the topic's
     * partition count (3, see {@code KafkaTopicConfig}).
     *
     * <p>The work here is dominated by a blocking call to the embedding sidecar, and that
     * sidecar is a synchronous FastAPI endpoint — so requests from separate listener threads
     * run in parallel in its worker pool rather than queueing. A single consumer therefore
     * leaves most of the machine idle: seeding a 5,183-document benchmark measured ~17
     * documents/minute at concurrency 1, with the sidecar using two cores out of ten.
     *
     * <p>Default stays 1 so a small deployment behaves exactly as before; raise it for bulk
     * ingestion. Ordering is unaffected — chunk upserts are idempotent by content hash
     * (docs/adr/0003), so per-partition ordering was never something this consumer relied on.
     */
    @KafkaListener(topics = "${recall.kafka.ingestion-topic}", groupId = "recall-ingestion",
            concurrency = "${recall.kafka.consumer-concurrency:1}")
    public void onMessage(String payload) {
        IngestionEvent event = parse(payload);
        String content = resolveContent(event);
        List<String> chunks = chunker.chunk(content);
        if (chunks.isEmpty()) {
            log.warn("doc {} produced no chunks — skipping", event.docId());
            return;
        }
        // Batched embedding call to the sidecar (blocking is fine on the listener thread).
        List<float[]> vectors = embeddings.embed(chunks).block();
        if (vectors == null || vectors.size() != chunks.size()) {
            throw new IllegalStateException("embedding count mismatch for " + event.docId());
        }
        for (int i = 0; i < chunks.size(); i++) {
            String chunk = chunks.get(i);
            String hash = Hashing.chunkId(event.docId(), i, chunk);
            index.upsert(new ChunkDocument(
                    event.docId(), i, chunk, event.source(), event.lang(), hash, vectors.get(i)));
        }
        meters.counter("recall.ingestion.chunks").increment(chunks.size());
        meters.counter("recall.ingestion.docs").increment();
        log.info("indexed {} chunks for doc {}", chunks.size(), event.docId());
    }

    private IngestionEvent parse(String payload) {
        IngestionEvent event;
        try {
            event = json.readValue(payload, IngestionEvent.class);
        } catch (JacksonException e) {
            throw new NonRetryableIngestionException("malformed ingestion payload", e);
        }
        if (event.docId() == null || event.docId().isBlank()) {
            throw new NonRetryableIngestionException("ingestion event without docId");
        }
        return event;
    }

    private String resolveContent(IngestionEvent event) {
        if (event.content() != null && !event.content().isBlank()) {
            return event.content();
        }
        if (event.objectKey() == null || event.objectKey().isBlank()) {
            throw new NonRetryableIngestionException(
                    "event for " + event.docId() + " has neither inline content nor objectKey");
        }
        return rawStore.fetch(event.objectKey());   // claim check; a fetch failure is retryable
    }
}
