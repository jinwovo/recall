package com.portfolio.recall.config;

import com.portfolio.recall.search.SearchMode;
import java.util.Locale;
import org.springframework.context.annotation.Configuration;
import org.springframework.format.FormatterRegistry;
import org.springframework.web.reactive.config.CorsRegistry;
import org.springframework.web.reactive.config.WebFluxConfigurer;

/** Dev CORS + custom converters for the WebFlux API. */
@Configuration
public class CorsConfig implements WebFluxConfigurer {

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
                .allowedOrigins("http://localhost:3000")
                .allowedMethods("GET", "POST", "PUT", "DELETE", "OPTIONS")
                .allowedHeaders("*");
    }

    /** Accept ?mode=hybrid / HYBRID / Hybrid alike. */
    @Override
    public void addFormatters(FormatterRegistry registry) {
        registry.addConverter(String.class, SearchMode.class,
                src -> SearchMode.valueOf(src.trim().toUpperCase(Locale.ROOT)));
    }
}
