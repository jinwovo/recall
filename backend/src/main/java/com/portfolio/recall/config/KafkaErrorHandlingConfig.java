package com.portfolio.recall.config;

import com.portfolio.recall.ingestion.NonRetryableIngestionException;
import io.micrometer.core.instrument.MeterRegistry;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.common.TopicPartition;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.listener.DeadLetterPublishingRecoverer;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.kafka.listener.RetryListener;
import org.springframework.kafka.support.ExponentialBackOffWithMaxRetries;

/**
 * Failure policy for the ingestion consumer (docs/adr/0005): transient failures (embedding
 * sidecar, ES, MinIO down) retry in place with exponential backoff; poison pills skip retries;
 * exhausted or non-retryable records are published to the dead-letter topic with forensic
 * headers (original topic/partition/offset, exception class + stacktrace) instead of being
 * dropped. Spring Boot wires this single {@code CommonErrorHandler} bean into every
 * {@code @KafkaListener} container.
 */
@Configuration
public class KafkaErrorHandlingConfig {

    private static final Logger log = LoggerFactory.getLogger(KafkaErrorHandlingConfig.class);

    @Bean
    public DefaultErrorHandler kafkaErrorHandler(KafkaTemplate<String, String> kafka,
                                                 RecallProperties props, MeterRegistry meters) {
        String dlqTopic = props.kafka().ingestionDlqTopic();
        var recoverer = new DeadLetterPublishingRecoverer(kafka,
                // Partition < 0 lets the producer pick — the DLQ need not mirror source partitions.
                (record, ex) -> new TopicPartition(dlqTopic, -1));

        var backOff = new ExponentialBackOffWithMaxRetries(props.kafka().retryMaxAttempts());
        backOff.setInitialInterval(props.kafka().retryBackoffMs());
        backOff.setMultiplier(2.0);

        var handler = new DefaultErrorHandler(recoverer, backOff);
        handler.addNotRetryableExceptions(NonRetryableIngestionException.class);
        handler.setRetryListeners(new RetryListener() {
            @Override
            public void failedDelivery(ConsumerRecord<?, ?> record, Exception ex, int deliveryAttempt) {
                if (deliveryAttempt > 1) {
                    meters.counter("recall.ingestion.retries").increment();
                }
            }

            @Override
            public void recovered(ConsumerRecord<?, ?> record, Exception ex) {
                meters.counter("recall.ingestion.dlq").increment();
                log.error("dead-lettered {}-{}@{} -> {}: {}", record.topic(), record.partition(),
                        record.offset(), dlqTopic, ex.getMessage());
            }

            @Override
            public void recoveryFailed(ConsumerRecord<?, ?> record, Exception original, Exception failure) {
                meters.counter("recall.ingestion.dlq.publish.failures").increment();
                log.error("DLQ publish failed for {}-{}@{}: {}", record.topic(), record.partition(),
                        record.offset(), failure.getMessage(), failure);
            }
        });
        return handler;
    }
}
