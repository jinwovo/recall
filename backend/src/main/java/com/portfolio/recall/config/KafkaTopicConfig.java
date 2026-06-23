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
}
