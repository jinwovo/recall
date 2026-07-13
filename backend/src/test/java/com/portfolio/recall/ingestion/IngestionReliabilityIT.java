package com.portfolio.recall.ingestion;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.portfolio.recall.config.KafkaErrorHandlingConfig;
import com.portfolio.recall.config.KafkaTopicConfig;
import com.portfolio.recall.config.MinioConfig;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.search.ChunkDocument;
import com.portfolio.recall.search.DocumentIndex;
import com.portfolio.recall.storage.RawDocumentStore;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.common.header.Header;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.ImportAutoConfiguration;
import org.springframework.boot.autoconfigure.jackson.JacksonAutoConfiguration;
import org.springframework.boot.autoconfigure.kafka.KafkaAutoConfiguration;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.containers.MinIOContainer;
import org.testcontainers.kafka.KafkaContainer;
import reactor.core.publisher.Mono;

/**
 * Validates ADR 0005 end-to-end against real Kafka + MinIO containers:
 *
 * <ul>
 *   <li>poison pills (malformed / unfulfillable events) dead-letter immediately, without retries;</li>
 *   <li>transient failures (embedding sidecar down) are retried with backoff and recover;</li>
 *   <li>exhausted retries dead-letter with forensic headers instead of dropping the record;</li>
 *   <li>large documents travel through Kafka as a MinIO objectKey (claim check) and are
 *       resolved back to full content by the consumer;</li>
 *   <li>small documents stay inline but are still archived to MinIO.</li>
 * </ul>
 *
 * <p>The Spring slice imports the production Kafka wiring (auto-configuration + the
 * {@link KafkaErrorHandlingConfig} error handler + topic beans) with the embedding client and
 * document index mocked, so what is under test is exactly the failure policy and the claim
 * check — not test doubles of them. Skipped automatically when Docker is unavailable.
 */
@Tag("integration")
@SpringBootTest(classes = IngestionReliabilityIT.TestApp.class)
class IngestionReliabilityIT {

    private static final String INGESTION_TOPIC = "recall.ingestion";
    private static final String DLQ_TOPIC = "recall.ingestion.dlq";
    private static final int MAX_RETRIES = 3;              // deliveries = 1 + MAX_RETRIES
    private static final int INLINE_MAX_BYTES = 256;

    private static KafkaContainer kafka = new KafkaContainer("apache/kafka:3.8.0");
    private static MinIOContainer minio = new MinIOContainer("minio/minio:RELEASE.2024-12-18T13-15-44Z");

    @Autowired private KafkaTemplate<String, String> template;
    @Autowired private IngestionService ingestionService;
    @Autowired private RawDocumentStore rawStore;
    @Autowired private EmbeddingClient embeddings;
    @Autowired private DocumentIndex index;

    @BeforeAll
    static void startContainers() {
        assumeTrue(DockerClientFactory.instance().isDockerAvailable(), "Docker required for this IT");
        kafka.start();
        minio.start();
    }

    @AfterAll
    static void stopContainers() {
        if (minio != null && minio.isRunning()) {
            minio.stop();
        }
        if (kafka != null && kafka.isRunning()) {
            kafka.stop();
        }
    }

    @DynamicPropertySource
    static void containerProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.kafka.bootstrap-servers", () -> kafka.getBootstrapServers());
        registry.add("recall.kafka.retry-max-attempts", () -> MAX_RETRIES);
        registry.add("recall.kafka.retry-backoff-ms", () -> 50);
        registry.add("recall.storage.endpoint", () -> minio.getS3URL());
        registry.add("recall.storage.access-key", () -> minio.getUserName());
        registry.add("recall.storage.secret-key", () -> minio.getPassword());
        registry.add("recall.storage.bucket", () -> "recall-raw-it");
        registry.add("recall.storage.inline-max-bytes", () -> INLINE_MAX_BYTES);
    }

    @BeforeEach
    void resetMocks() {
        reset(embeddings, index);
    }

    // ---- failure policy ----

    @Test
    void malformedPayloadDeadLettersImmediatelyWithoutRetry() {
        String docId = "poison-json-" + UUID.randomUUID();
        template.send(INGESTION_TOPIC, docId, "{\"docId\":\"" + docId + "\"  << not json");

        ConsumerRecord<String, String> dead = awaitDlqRecord(docId, Duration.ofSeconds(20));

        assertThat(dead).as("poison pill must land on the DLQ").isNotNull();
        assertThat(headerText(dead)).contains("NonRetryableIngestionException");
        assertThat(headerValue(dead, "kafka_dlt-original-topic")).isEqualTo(INGESTION_TOPIC);
        verify(embeddings, never()).embed(anyList());   // no retries burned on a poison pill
    }

    @Test
    void eventWithoutContentOrObjectKeyDeadLettersImmediately() {
        String docId = "poison-empty-" + UUID.randomUUID();
        template.send(INGESTION_TOPIC, docId, "{\"docId\":\"" + docId + "\"}");

        ConsumerRecord<String, String> dead = awaitDlqRecord(docId, Duration.ofSeconds(20));

        assertThat(dead).isNotNull();
        assertThat(headerText(dead)).contains("NonRetryableIngestionException");
        verify(embeddings, never()).embed(anyList());
    }

    @Test
    void transientEmbeddingFailureIsRetriedThenIndexed() {
        String docId = "transient-" + UUID.randomUUID();
        AtomicInteger calls = new AtomicInteger();
        when(embeddings.embed(anyList())).thenAnswer(inv -> {
            if (calls.incrementAndGet() <= 2) {
                throw new IllegalStateException("embedding sidecar down (simulated)");
            }
            return Mono.just(vectorsFor(inv.getArgument(0)));
        });

        ingestionService.enqueue(new IngestionEvent(docId, "it://reliability", "en",
                "Spring Kafka retries transient failures with exponential backoff."));

        verify(index, timeout(30_000).atLeastOnce()).upsert(Mockito.any(ChunkDocument.class));
        assertThat(calls.get()).isEqualTo(3);           // failed, failed, recovered
        assertThat(awaitDlqRecord(docId, Duration.ofSeconds(2)))
                .as("recovered record must not be dead-lettered").isNull();
    }

    @Test
    void exhaustedRetriesDeadLetterWithForensicHeaders() {
        String docId = "exhausted-" + UUID.randomUUID();
        when(embeddings.embed(anyList()))
                .thenThrow(new IllegalStateException("embedding sidecar down (simulated)"));

        ingestionService.enqueue(new IngestionEvent(docId, "it://reliability", "en",
                "This document can never be embedded."));

        ConsumerRecord<String, String> dead = awaitDlqRecord(docId, Duration.ofSeconds(30));

        assertThat(dead).isNotNull();
        verify(embeddings, timeout(5_000).times(1 + MAX_RETRIES)).embed(anyList());
        assertThat(headerText(dead)).contains("IllegalStateException");
        assertThat(headerValue(dead, "kafka_dlt-original-topic")).isEqualTo(INGESTION_TOPIC);
        assertThat(dead.value()).contains(docId);       // payload preserved for replay
        verify(index, never()).upsert(Mockito.any(ChunkDocument.class));
    }

    // ---- claim check / raw archive ----

    @Test
    void largeDocumentTravelsAsObjectKeyAndIsResolvedFromMinio() throws Exception {
        String docId = "large-" + UUID.randomUUID();
        String content = "쿠버네티스 파드 오토스케일링. Kubernetes autoscaling docs. ".repeat(200); // ≫ inline cap
        when(embeddings.embed(anyList())).thenAnswer(inv -> Mono.just(vectorsFor(inv.getArgument(0))));

        ingestionService.enqueue(new IngestionEvent(docId, "it://claim-check", "ko", content));

        // 1) The wire event carries a reference, not the payload.
        ConsumerRecord<String, String> wire = awaitRecord(INGESTION_TOPIC, docId, Duration.ofSeconds(20));
        assertThat(wire).isNotNull();
        assertThat(wire.value()).contains("\"objectKey\"").doesNotContain(content.substring(0, 80));

        // 2) The raw document is archived and byte-identical.
        assertThat(rawStore.fetch(RawDocumentStore.objectKey(docId))).isEqualTo(content);

        // 3) The consumer resolves the reference and indexes the full content.
        ArgumentCaptor<ChunkDocument> chunks = ArgumentCaptor.forClass(ChunkDocument.class);
        verify(index, timeout(30_000).atLeast(2)).upsert(chunks.capture());
        String reassembled = String.join("", chunks.getAllValues().stream()
                .filter(c -> c.docId().equals(docId))
                .map(ChunkDocument::content).toList());
        assertThat(reassembled).contains("Kubernetes autoscaling docs");
        assertThat(chunks.getAllValues().getFirst().content())
                .startsWith(content.strip().substring(0, 40));
    }

    @Test
    void smallDocumentStaysInlineAndIsStillArchived() throws Exception {
        String docId = "small-" + UUID.randomUUID();
        String content = "Tiny doc — travels inline.";
        when(embeddings.embed(anyList())).thenAnswer(inv -> Mono.just(vectorsFor(inv.getArgument(0))));

        ingestionService.enqueue(new IngestionEvent(docId, "it://claim-check", "en", content));

        ConsumerRecord<String, String> wire = awaitRecord(INGESTION_TOPIC, docId, Duration.ofSeconds(20));
        assertThat(wire).isNotNull();
        assertThat(wire.value()).contains(content).contains("\"objectKey\"");
        assertThat(rawStore.fetch(RawDocumentStore.objectKey(docId))).isEqualTo(content);
        verify(index, timeout(30_000).atLeastOnce()).upsert(Mockito.any(ChunkDocument.class));
    }

    // ---- helpers ----

    private static List<float[]> vectorsFor(List<String> chunks) {
        List<float[]> vectors = new ArrayList<>(chunks.size());
        for (int i = 0; i < chunks.size(); i++) {
            vectors.add(new float[] {1f, 0f, 0f, 0f});
        }
        return vectors;
    }

    private ConsumerRecord<String, String> awaitDlqRecord(String key, Duration timeout) {
        return awaitRecord(DLQ_TOPIC, key, timeout);
    }

    /** Polls the topic from the beginning with a throwaway group until a record with the key appears. */
    private ConsumerRecord<String, String> awaitRecord(String topic, String key, Duration timeout) {
        Map<String, Object> config = Map.of(
                ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, kafka.getBootstrapServers(),
                ConsumerConfig.GROUP_ID_CONFIG, "it-probe-" + UUID.randomUUID(),
                ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest",
                ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class,
                ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class);
        try (KafkaConsumer<String, String> probe = new KafkaConsumer<>(config)) {
            probe.subscribe(List.of(topic));
            long deadline = System.currentTimeMillis() + timeout.toMillis();
            while (System.currentTimeMillis() < deadline) {
                for (ConsumerRecord<String, String> record : probe.poll(Duration.ofMillis(250))) {
                    if (key.equals(record.key())) {
                        return record;
                    }
                }
            }
            return null;
        }
    }

    private static String headerValue(ConsumerRecord<String, String> record, String name) {
        Header header = record.headers().lastHeader(name);
        return header == null ? null : new String(header.value(), StandardCharsets.UTF_8);
    }

    private static String headerText(ConsumerRecord<String, String> record) {
        StringBuilder sb = new StringBuilder();
        for (Header h : record.headers()) {
            sb.append(h.key()).append('=')
              .append(new String(h.value(), StandardCharsets.UTF_8)).append('\n');
        }
        return sb.toString();
    }

    /**
     * Minimal slice: real Kafka auto-configuration + the production error handler, topics,
     * MinIO client and store; embedding + index mocked so failures are scriptable.
     */
    @SpringBootConfiguration
    @ImportAutoConfiguration({KafkaAutoConfiguration.class, JacksonAutoConfiguration.class})
    @EnableConfigurationProperties(RecallProperties.class)
    @Import({IngestionService.class, IngestionConsumer.class, Chunker.class,
            KafkaTopicConfig.class, KafkaErrorHandlingConfig.class, MinioConfig.class,
            RawDocumentStore.class})
    static class TestApp {

        @Bean
        MeterRegistry meterRegistry() {
            return new SimpleMeterRegistry();
        }

        @Bean
        EmbeddingClient embeddingClient() {
            return Mockito.mock(EmbeddingClient.class);
        }

        @Bean
        DocumentIndex documentIndex() {
            return Mockito.mock(DocumentIndex.class);
        }
    }
}
