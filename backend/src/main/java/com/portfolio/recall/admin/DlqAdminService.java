package com.portfolio.recall.admin;

import com.portfolio.recall.admin.DlqSnapshot.DlqRecord;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.consumer.Consumer;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.ConsumerRecords;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.PartitionInfo;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.header.Header;
import org.apache.kafka.common.header.Headers;
import org.apache.kafka.common.header.internals.RecordHeaders;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.ConsumerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.KafkaHeaders;
import org.springframework.stereotype.Service;

/**
 * Ops surface for the ingestion dead-letter topic (docs/adr/0006): inspect what failed and
 * why (forensic headers from docs/adr/0005), then drain the backlog back onto the ingestion
 * topic once the underlying fault is fixed.
 *
 * <p>Replay is a bounded consumer-group drain: records are re-published to the ingestion
 * topic (broker-acked, provenance headers attached, {@code kafka_dlt-*} forensics stripped so
 * a re-failure gets fresh ones) and only then are their DLQ offsets committed. A crash between
 * publish and commit re-replays the record — at-least-once is the deliberate choice, because
 * indexing is idempotent by content hash (docs/adr/0003) and a duplicate replay converges to
 * the same index state. Records that fail again (e.g. a replayed poison pill) simply
 * dead-letter again with their replay count intact — replay never loses the forensic chain.
 *
 * <p>Inspection uses the replay group's committed offsets only to compute the pending
 * backlog; it never commits, so peeking cannot skip records. Consumers are created per call
 * with {@code enable.auto.commit=false} — the broker default of auto-commit would silently
 * advance the replay group while peeking. Replay is serialized per instance; concurrent
 * drains from multiple replicas would duplicate work but stay correct (idempotent sink).
 */
@Service
public class DlqAdminService {

    /** Provenance carried by replayed records (and preserved by the recoverer on re-failure). */
    static final String HEADER_REPLAYS = "recall_dlq-replays";
    static final String HEADER_REPLAY_SOURCE = "recall_dlq-replay-source";

    private static final Logger log = LoggerFactory.getLogger(DlqAdminService.class);
    private static final String REPLAY_GROUP = "recall-dlq-replay";
    private static final Duration POLL = Duration.ofMillis(400);
    private static final Duration INSPECT_DEADLINE = Duration.ofSeconds(10);
    private static final long SEND_TIMEOUT_SECONDS = 10;
    private static final int PREVIEW_CHARS = 240;

    private final ConsumerFactory<String, String> consumers;
    private final KafkaTemplate<String, String> kafka;
    private final MeterRegistry meters;
    private final String dlqTopic;
    private final String ingestionTopic;

    public DlqAdminService(ConsumerFactory<String, String> consumers,
                           KafkaTemplate<String, String> kafka,
                           MeterRegistry meters, RecallProperties props) {
        this.consumers = consumers;
        this.kafka = kafka;
        this.meters = meters;
        this.dlqTopic = props.kafka().ingestionDlqTopic();
        this.ingestionTopic = props.kafka().ingestionTopic();
    }

    /** Bounded, commit-free peek from the head of the DLQ plus the replay group's backlog. */
    public DlqSnapshot inspect(int limit) {
        try (Consumer<String, String> consumer = adminConsumer("peek")) {
            List<TopicPartition> partitions = partitionsOf(consumer);
            if (partitions.isEmpty()) {
                return new DlqSnapshot(0, List.of());
            }
            consumer.assign(partitions);
            Map<TopicPartition, Long> begin = consumer.beginningOffsets(partitions);
            Map<TopicPartition, Long> end = consumer.endOffsets(partitions);
            Map<TopicPartition, Long> next = nextToReplay(consumer, partitions, begin);

            consumer.seekToBeginning(partitions);
            List<DlqRecord> records = new ArrayList<>();
            long deadline = System.nanoTime() + INSPECT_DEADLINE.toNanos();
            while (records.size() < limit && !reachedEnd(consumer, end) && System.nanoTime() < deadline) {
                ConsumerRecords<String, String> batch = consumer.poll(POLL);
                for (ConsumerRecord<String, String> record : batch) {
                    if (records.size() >= limit) {
                        break;
                    }
                    records.add(toView(record, next));
                }
            }
            return new DlqSnapshot(backlog(end, next), records);
        }
    }

    /**
     * Drains up to {@code max} pending records back onto the ingestion topic. Progress is
     * committed even if a publish fails mid-drain, so successfully replayed records are
     * never replayed twice by the next invocation.
     */
    public synchronized DlqReplayResult replay(int max) {
        try (Consumer<String, String> consumer = adminConsumer("replay")) {
            List<TopicPartition> partitions = partitionsOf(consumer);
            if (partitions.isEmpty()) {
                return new DlqReplayResult(0, 0);
            }
            consumer.assign(partitions);
            Map<TopicPartition, Long> begin = consumer.beginningOffsets(partitions);
            Map<TopicPartition, Long> end = consumer.endOffsets(partitions);
            Map<TopicPartition, Long> next = nextToReplay(consumer, partitions, begin);
            next.forEach(consumer::seek);

            Map<TopicPartition, OffsetAndMetadata> progress = new HashMap<>();
            int replayed = 0;
            try {
                int idlePolls = 0;
                while (replayed < max && idlePolls < 2 && !reachedEnd(consumer, end)) {
                    ConsumerRecords<String, String> batch = consumer.poll(POLL);
                    if (batch.isEmpty()) {
                        idlePolls++;
                        continue;
                    }
                    idlePolls = 0;
                    for (ConsumerRecord<String, String> record : batch) {
                        if (replayed >= max) {
                            break;
                        }
                        republish(record);
                        TopicPartition tp = new TopicPartition(record.topic(), record.partition());
                        progress.put(tp, new OffsetAndMetadata(record.offset() + 1));
                        next.put(tp, record.offset() + 1);
                        replayed++;
                        meters.counter("recall.ingestion.dlq.replayed").increment();
                    }
                }
            } finally {
                if (!progress.isEmpty()) {
                    consumer.commitSync(progress);
                }
            }
            log.info("replayed {} DLQ record(s) to {} ({} remaining)", replayed, ingestionTopic,
                    backlog(end, next));
            return new DlqReplayResult(replayed, backlog(end, next));
        }
    }

    /** Re-publish with fresh provenance; forensic {@code kafka_dlt-*} headers are re-earned, not inherited. */
    private void republish(ConsumerRecord<String, String> record) {
        Headers headers = new RecordHeaders();
        int replays = 0;
        for (Header header : record.headers()) {
            if (header.key().equals(HEADER_REPLAYS)) {
                replays = parseIntOrZero(header.value());
            } else if (!header.key().startsWith("kafka_dlt-") && !header.key().equals(HEADER_REPLAY_SOURCE)) {
                headers.add(header);
            }
        }
        headers.add(HEADER_REPLAYS, utf8(Integer.toString(replays + 1)));
        headers.add(HEADER_REPLAY_SOURCE, utf8(record.topic() + "-" + record.partition() + "@" + record.offset()));
        try {
            kafka.send(new ProducerRecord<>(ingestionTopic, null, record.key(), record.value(), headers))
                    .get(SEND_TIMEOUT_SECONDS, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted replaying DLQ offset " + record.offset(), e);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to replay DLQ offset " + record.offset(), e);
        }
    }

    private DlqRecord toView(ConsumerRecord<String, String> record, Map<TopicPartition, Long> next) {
        Headers headers = record.headers();
        String exceptionType = firstNonNull(
                stringHeader(headers, KafkaHeaders.DLT_EXCEPTION_CAUSE_FQCN),
                stringHeader(headers, KafkaHeaders.DLT_EXCEPTION_FQCN));
        long nextOffset = next.getOrDefault(new TopicPartition(record.topic(), record.partition()), 0L);
        return new DlqRecord(
                record.key(),
                record.partition(),
                record.offset(),
                Instant.ofEpochMilli(record.timestamp()),
                record.offset() >= nextOffset,
                stringHeader(headers, KafkaHeaders.DLT_ORIGINAL_TOPIC),
                longHeader(headers, KafkaHeaders.DLT_ORIGINAL_OFFSET),
                exceptionType,
                stringHeader(headers, KafkaHeaders.DLT_EXCEPTION_MESSAGE),
                parseIntOrZero(rawHeader(headers, HEADER_REPLAYS)),
                preview(record.value()));
    }

    /** Per partition, the first offset the replay group has not consumed yet. */
    private Map<TopicPartition, Long> nextToReplay(Consumer<String, String> consumer,
                                                   List<TopicPartition> partitions,
                                                   Map<TopicPartition, Long> begin) {
        Map<TopicPartition, OffsetAndMetadata> committed = consumer.committed(new HashSet<>(partitions));
        Map<TopicPartition, Long> next = new HashMap<>();
        for (TopicPartition tp : partitions) {
            OffsetAndMetadata c = committed.get(tp);
            next.put(tp, c != null ? c.offset() : begin.getOrDefault(tp, 0L));
        }
        return next;
    }

    private List<TopicPartition> partitionsOf(Consumer<String, String> consumer) {
        List<PartitionInfo> infos = consumer.partitionsFor(dlqTopic);
        if (infos == null) {
            return List.of();
        }
        return infos.stream().map(i -> new TopicPartition(i.topic(), i.partition())).toList();
    }

    private Consumer<String, String> adminConsumer(String role) {
        Properties overrides = new Properties();
        // The broker default (auto-commit on) would advance the replay group as a side effect.
        overrides.setProperty(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, "false");
        return consumers.createConsumer(REPLAY_GROUP, "dlq-admin-" + role, null, overrides);
    }

    private static long backlog(Map<TopicPartition, Long> end, Map<TopicPartition, Long> next) {
        long depth = 0;
        for (Map.Entry<TopicPartition, Long> e : end.entrySet()) {
            depth += Math.max(0, e.getValue() - next.getOrDefault(e.getKey(), 0L));
        }
        return depth;
    }

    private static boolean reachedEnd(Consumer<String, String> consumer, Map<TopicPartition, Long> end) {
        return end.entrySet().stream().allMatch(e -> consumer.position(e.getKey()) >= e.getValue());
    }

    private static String preview(String value) {
        if (value == null) {
            return null;
        }
        return value.length() <= PREVIEW_CHARS ? value : value.substring(0, PREVIEW_CHARS) + "…";
    }

    private static byte[] rawHeader(Headers headers, String name) {
        Header header = headers.lastHeader(name);
        return header == null ? null : header.value();
    }

    private static String stringHeader(Headers headers, String name) {
        byte[] value = rawHeader(headers, name);
        return value == null ? null : new String(value, StandardCharsets.UTF_8);
    }

    /** The recoverer writes original partition/offset as big-endian int/long bytes. */
    private static Long longHeader(Headers headers, String name) {
        byte[] value = rawHeader(headers, name);
        if (value == null) {
            return null;
        }
        return switch (value.length) {
            case Long.BYTES -> ByteBuffer.wrap(value).getLong();
            case Integer.BYTES -> (long) ByteBuffer.wrap(value).getInt();
            default -> null;
        };
    }

    private static int parseIntOrZero(byte[] value) {
        if (value == null) {
            return 0;
        }
        try {
            return Integer.parseInt(new String(value, StandardCharsets.UTF_8).trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static String firstNonNull(String a, String b) {
        return a != null ? a : b;
    }

    private static byte[] utf8(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }
}
