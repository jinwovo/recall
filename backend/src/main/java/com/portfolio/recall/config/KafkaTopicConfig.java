package com.portfolio.recall.config;

import org.apache.kafka.clients.admin.NewTopic;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.config.TopicBuilder;

@Configuration
public class KafkaTopicConfig {

    @Bean
    public NewTopic ingestionTopic(RecallProperties props) {
        return TopicBuilder.name(props.kafka().ingestionTopic()).partitions(3).replicas(1).build();
    }

    /** Dead-letter destination for poison pills / exhausted retries (docs/adr/0005). */
    @Bean
    public NewTopic ingestionDlqTopic(RecallProperties props) {
        return TopicBuilder.name(props.kafka().ingestionDlqTopic()).partitions(1).replicas(1).build();
    }
}
