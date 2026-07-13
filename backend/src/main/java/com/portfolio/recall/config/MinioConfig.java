package com.portfolio.recall.config;

import io.minio.MinioClient;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class MinioConfig {

    @Bean
    public MinioClient minioClient(RecallProperties props) {
        return MinioClient.builder()
                .endpoint(props.storage().endpoint())
                .credentials(props.storage().accessKey(), props.storage().secretKey())
                .build();
    }
}
