package com.portfolio.recall.ingestion;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.RecallProperties;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;

/** Accepts a document and publishes it to Kafka for async processing (docs/adr/0003). */
@Service
public class IngestionService {

    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper json;
    private final String topic;

    public IngestionService(KafkaTemplate<String, String> kafka, ObjectMapper json, RecallProperties props) {
        this.kafka = kafka;
        this.json = json;
        this.topic = props.kafka().ingestionTopic();
    }

    public void enqueue(IngestionEvent event) {
        try {
            // TODO: persist raw content to MinIO; publish an objectKey for large docs.
            kafka.send(topic, event.docId(), json.writeValueAsString(event));
        } catch (Exception e) {
            throw new IllegalStateException("Failed to enqueue ingestion for " + event.docId(), e);
        }
    }
}
