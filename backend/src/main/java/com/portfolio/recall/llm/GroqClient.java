package com.portfolio.recall.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.RecallProperties;
import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;

/**
 * Free LLM provider via Groq's OpenAI-compatible Chat Completions API (SSE streaming).
 * Active when {@code recall.llm.provider=groq}. Ignores {@link ModelTier} (single configured model).
 */
@Component
@ConditionalOnProperty(name = "recall.llm.provider", havingValue = "groq")
public class GroqClient implements LlmClient {

    private final WebClient web;
    private final String model;
    private final ObjectMapper json;

    public GroqClient(WebClient.Builder builder, RecallProperties props, ObjectMapper json,
                      @Value("${GROQ_API_KEY:}") String apiKey) {
        this.web = builder
                .baseUrl(props.llm().groq().baseUrl())
                .defaultHeader("Authorization", "Bearer " + apiKey)
                .build();
        this.model = props.llm().groq().model();
        this.json = json;
    }

    @Override
    public Flux<String> streamAnswer(String system, String userPrompt, ModelTier tier) {
        Map<String, Object> body = Map.of(
                "model", model,
                "stream", true,
                "messages", List.of(
                        Map.of("role", "system", "content", system),
                        Map.of("role", "user", "content", userPrompt)));

        return web.post()
                .uri("/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(body)
                .retrieve()
                .bodyToFlux(new ParameterizedTypeReference<ServerSentEvent<String>>() {})
                .map(ServerSentEvent::data)
                .filter(d -> d != null)
                .takeWhile(d -> !"[DONE]".equals(d))
                .mapNotNull(this::extractDelta)
                .filter(s -> !s.isEmpty());
    }

    /** Pull choices[0].delta.content from one streamed chunk. */
    private String extractDelta(String data) {
        try {
            JsonNode content = json.readTree(data).path("choices").path(0).path("delta").path("content");
            return content.isTextual() ? content.asText() : "";
        } catch (Exception e) {
            return "";
        }
    }
}
