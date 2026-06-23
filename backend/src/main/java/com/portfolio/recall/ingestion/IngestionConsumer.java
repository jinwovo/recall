package com.portfolio.recall.ingestion;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.common.Hashing;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.search.ChunkDocument;
import com.portfolio.recall.search.DocumentIndex;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * Consumes ingestion events and runs the pipeline: chunk → embed (batched) → idempotent upsert.
 * Idempotency: each chunk's ES _id is sha256(docId + index + content), so retries/duplicates
 * and concurrent ingestion converge to the same index state (docs/adr/0003).
 */
@Component
public class IngestionConsumer {

    private static final Logger log = LoggerFactory.getLogger(IngestionConsumer.class);

    private final Chunker chunker;
    private final EmbeddingClient embeddings;
    private final DocumentIndex index;
    private final ObjectMapper json;
    private final MeterRegistry meters;

    public IngestionConsumer(Chunker chunker, EmbeddingClient embeddings, DocumentIndex index,
                             ObjectMapper json, MeterRegistry meters) {
        this.chunker = chunker;
        this.embeddings = embeddings;
        this.index = index;
        this.json = json;
        this.meters = meters;
    }

    @KafkaListener(topics = "${recall.kafka.ingestion-topic}", groupId = "recall-ingestion")
    public void onMessage(String payload) {
        try {
            IngestionEvent event = json.readValue(payload, IngestionEvent.class);
            List<String> chunks = chunker.chunk(event.content());
            if (chunks.isEmpty()) {
                return;
            }
            // Batched embedding call to the sidecar (blocking is fine on the listener thread).
            List<float[]> vectors = embeddings.embed(chunks).block();
            if (vectors == null || vectors.size() != chunks.size()) {
                throw new IllegalStateException("embedding count mismatch for " + event.docId());
            }
            for (int i = 0; i < chunks.size(); i++) {
                String content = chunks.get(i);
                String hash = Hashing.chunkId(event.docId(), i, content);
                index.upsert(new ChunkDocument(
                        event.docId(), i, content, event.source(), event.lang(), hash, vectors.get(i)));
            }
            meters.counter("recall.ingestion.chunks").increment(chunks.size());
            meters.counter("recall.ingestion.docs").increment();
            log.info("indexed {} chunks for doc {}", chunks.size(), event.docId());
        } catch (Exception e) {
            // TODO: dead-letter topic + retry policy.
            log.error("ingestion failed: {}", e.getMessage(), e);
        }
    }

}
