package com.portfolio.recall;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

@SpringBootApplication
@ConfigurationPropertiesScan
public class RecallApplication {
    public static void main(String[] args) {
        SpringApplication.run(RecallApplication.class, args);
    }
}
