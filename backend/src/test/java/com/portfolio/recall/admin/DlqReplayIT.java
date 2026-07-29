package com.portfolio.recall.admin;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.fail;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.portfolio.recall.admin.DlqSnapshot.DlqRecord;
import com.portfolio.recall.config.KafkaErrorHandlingConfig;
import com.portfolio.recall.config.KafkaTopicConfig;
import com.portfolio.recall.config.MinioConfig;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.ingestion.Chunker;
import com.portfolio.recall.ingestion.IngestionConsumer;
import com.portfolio.recall.ingestion.IngestionEvent;
import com.portfolio.recall.ingestion.IngestionService;
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
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Predicate;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.common.header.Header;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer.OrderAnnotation;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
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
 * Validates ADR 0006 end-to-end against real Kafka + MinIO: the DLQ is not a grave but a
 * queue — after the underlying fault is fixed, {@link DlqAdminService#replay} drains the
 * backlog onto the ingestion topic and the documents end up indexed.
 *
 * <ul>
 *   <li>dead-lettered record is visible via {@code inspect} with decoded forensic headers;</li>
 *   <li>after the fault is fixed, {@code replay} re-publishes it (provenance headers on,
 *       stale {@code kafka_dlt-*} forensics off) and the document is indexed;</li>
 *   <li>the drain commits — a second replay finds nothing, and the record stays visible
 *       in the peek as {@code pending=false};</li>
 *   <li>a replayed poison pill dead-letters again with its replay count incremented —
 *       replay cannot lose a record, only round-trip it with a bigger forensic trail.</li>
 * </ul>
 *
 * <p>Same slice philosophy as {@code IngestionReliabilityIT}: production Kafka wiring, error
 * handler, topics, MinIO store and the admin service are real; embedding + index are mocked
 * so faults are scriptable. Tests are ordered — they share the class-scoped topics, and the
 * strict depth assertions rely on the previous test leaving the DLQ drained.
 */
@Tag("integration")
@SpringBootTest(classes = DlqReplayIT.TestApp.class)
@TestMethodOrder(OrderAnnotation.class)
class DlqReplayIT {

    private static final String INGESTION_TOPIC = "recall.ingestion";
    private static final String DLQ_TOPIC = "recall.ingestion.dlq";

    private static KafkaContainer kafka = new KafkaContainer("apache/kafka:3.8.0");
    private static MinIOContainer minio = new MinIOContainer("minio/minio:RELEASE.2024-12-18T13-15-44Z");

    @Autowired private KafkaTemplate<String, String> template;
    @Autowired private IngestionService ingestionService;
    @Autowired private DlqAdminService admin;
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
        registry.add("recall.kafka.retry-max-attempts", () -> 1);   // retry policy itself is covered by IngestionReliabilityIT
        registry.add("recall.kafka.retry-backoff-ms", () -> 50);
        registry.add("recall.storage.endpoint", () -> minio.getS3URL());
        registry.add("recall.storage.access-key", () -> minio.getUserName());
        registry.add("recall.storage.secret-key", () -> minio.getPassword());
        registry.add("recall.storage.bucket", () -> "recall-raw-replay-it");
        registry.add("recall.storage.inline-max-bytes", () -> 65536);
    }

    @BeforeEach
    void resetMocks() {
        reset(embeddings, index);
    }

    @Test
    @Order(1)
    void deadLetteredDocumentIsReplayedAndIndexedAfterFixingTheFault() {
        String docId = "replay-" + UUID.randomUUID();
        AtomicBoolean sidecarFixed = new AtomicBoolean(false);
        when(embeddings.embed(anyList())).thenAnswer(inv -> {
            if (!sidecarFixed.get()) {
                throw new IllegalStateException("embedding sidecar down (simulated)");
            }
            return Mono.just(vectorsFor(inv.getArgument(0)));
        });

        ingestionService.enqueue(new IngestionEvent(docId, "it://dlq-replay", "en",
                "Replay drains the dead-letter backlog once the fault is fixed."));

        // 1) The failure is visible on the admin surface with decoded forensics.
        DlqSnapshot snapshot = awaitDepth(1, Duration.ofSeconds(30));
        DlqRecord dead = recordFor(snapshot, docId);
        assertThat(dead.pending()).isTrue();
        assertThat(dead.originalTopic()).isEqualTo(INGESTION_TOPIC);
        assertThat(dead.exceptionType()).contains("IllegalStateException");
        assertThat(dead.replays()).isZero();
        assertThat(dead.payloadPreview()).contains(docId);

        // 2) Fix the fault, drain the backlog.
        sidecarFixed.set(true);
        DlqReplayResult result = admin.replay(100);
        assertThat(result.replayed()).isEqualTo(1);
        assertThat(result.remaining()).isZero();

        // 3) The replayed wire record carries provenance, not stale forensics.
        ConsumerRecord<String, String> wire = awaitRecord(INGESTION_TOPIC, docId, Duration.ofSeconds(20),
                r -> r.headers().lastHeader(DlqAdminService.HEADER_REPLAYS) != null);
        assertThat(wire).as("replayed record must reach the ingestion topic").isNotNull();
        assertThat(headerText(wire, DlqAdminService.HEADER_REPLAYS)).isEqualTo("1");
        assertThat(headerText(wire, DlqAdminService.HEADER_REPLAY_SOURCE)).startsWith(DLQ_TOPIC + "-");
        assertThat(headerKeys(wire)).noneMatch(k -> k.startsWith("kafka_dlt-"));

        // 4) This time the pipeline succeeds end-to-end.
        ArgumentCaptor<ChunkDocument> chunks = ArgumentCaptor.forClass(ChunkDocument.class);
        verify(index, timeout(30_000).atLeastOnce()).upsert(chunks.capture());
        assertThat(chunks.getAllValues()).anyMatch(c -> c.docId().equals(docId));

        // 5) The drain committed: nothing left to replay, record stays visible as history.
        assertThat(admin.replay(100).replayed()).isZero();
        DlqSnapshot after = admin.inspect(50);
        assertThat(after.depth()).isZero();
        assertThat(recordFor(after, docId).pending()).isFalse();
    }

    @Test
    @Order(2)
    void replayedPoisonPillDeadLettersAgainWithReplayCountIncremented() {
        String docId = "poison-" + UUID.randomUUID();
        template.send(INGESTION_TOPIC, docId, "{\"docId\":\"" + docId + "\"  << not json");

        DlqSnapshot before = awaitDepth(1, Duration.ofSeconds(30));
        assertThat(recordFor(before, docId).replays()).isZero();

        // Replaying without fixing anything must not lose the record — it round-trips.
        assertThat(admin.replay(100).replayed()).isEqualTo(1);

        ConsumerRecord<String, String> secondDeath = awaitRecord(DLQ_TOPIC, docId, Duration.ofSeconds(30),
                r -> "1".equals(headerText(r, DlqAdminService.HEADER_REPLAYS)));
        assertThat(secondDeath).as("replayed poison pill must dead-letter again").isNotNull();
        assertThat(headerKeys(secondDeath)).anyMatch(k -> k.startsWith("kafka_dlt-"));   // fresh forensics

        DlqSnapshot after = awaitDepth(1, Duration.ofSeconds(10));
        assertThat(recordFor(after, docId).replays()).isEqualTo(1);
        verify(embeddings, never()).embed(anyList());
    }

    // ---- helpers ----

    private static List<float[]> vectorsFor(List<String> chunks) {
        List<float[]> vectors = new ArrayList<>(chunks.size());
        for (int i = 0; i < chunks.size(); i++) {
            vectors.add(new float[] {1f, 0f, 0f, 0f});
        }
        return vectors;
    }

    private DlqSnapshot awaitDepth(long expected, Duration timeout) {
        long deadline = System.currentTimeMillis() + timeout.toMillis();
        DlqSnapshot last = null;
        while (System.currentTimeMillis() < deadline) {
            last = admin.inspect(50);
            if (last.depth() == expected) {
                return last;
            }
            try {
                Thread.sleep(200);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        fail("DLQ depth did not reach " + expected + " in " + timeout + "; last=" + last);
        return null;   // unreachable
    }

    /** Peek rows for the record under test — other rows are history from earlier tests. */
    private static DlqRecord recordFor(DlqSnapshot snapshot, String docId) {
        List<DlqRecord> matches = snapshot.records().stream()
                .filter(r -> docId.equals(r.key())).toList();
        assertThat(matches).as("DLQ peek must contain %s", docId).isNotEmpty();
        return matches.getLast();   // latest incarnation (poison pills reappear after replay)
    }

    /** Polls the topic from the beginning with a throwaway group until a matching record appears. */
    private ConsumerRecord<String, String> awaitRecord(String topic, String key, Duration timeout,
                                                       Predicate<ConsumerRecord<String, String>> filter) {
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
                    if (key.equals(record.key()) && filter.test(record)) {
                        return record;
                    }
                }
            }
            return null;
        }
    }

    private static String headerText(ConsumerRecord<String, String> record, String name) {
        Header header = record.headers().lastHeader(name);
        return header == null ? null : new String(header.value(), StandardCharsets.UTF_8);
    }

    private static List<String> headerKeys(ConsumerRecord<String, String> record) {
        List<String> keys = new ArrayList<>();
        for (Header h : record.headers()) {
            keys.add(h.key());
        }
        return keys;
    }

    /**
     * Minimal slice: real Kafka auto-configuration, error handler, topics, MinIO store and
     * the admin service under test; embedding + index mocked so faults are scriptable.
     */
    @SpringBootConfiguration
    @ImportAutoConfiguration({KafkaAutoConfiguration.class, JacksonAutoConfiguration.class})
    @EnableConfigurationProperties(RecallProperties.class)
    @Import({IngestionService.class, IngestionConsumer.class, Chunker.class,
            KafkaTopicConfig.class, KafkaErrorHandlingConfig.class, MinioConfig.class,
            RawDocumentStore.class, DlqAdminService.class})
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
