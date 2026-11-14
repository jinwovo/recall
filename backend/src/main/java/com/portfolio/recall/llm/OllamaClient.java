package com.portfolio.recall.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;

/**
 * Free, fully-local LLM provider via Ollama's OpenAI-compatible API (SSE streaming).
 * Active when {@code recall.llm.provider=ollama}. No API key or account required.
 */
@Component
@ConditionalOnProperty(name = "recall.llm.provider", havingValue = "ollama")
public class OllamaClient implements LlmClient {

    private final WebClient web;
    private final String model;
    private final long maxTokens;
    private final ObjectMapper json;
    private final MeterRegistry meters;

    public OllamaClient(WebClient.Builder builder, RecallProperties props, ObjectMapper json,
                      MeterRegistry meters) {
        this.web = builder.baseUrl(props.llm().ollama().baseUrl()).build();
        this.model = props.llm().ollama().model();
        this.maxTokens = props.llm().maxTokens();
        this.json = json;
        this.meters = meters;
    }

    @Override
    public Flux<StreamChunk> stream(String system, String userPrompt, ModelTier tier) {
        Map<String, Object> body = Map.of(
                "model", model,
                "stream", true,
                // Without this Ollama sends no usage at all; with it, one final chunk does.
                "stream_options", Map.of("include_usage", true),
                "max_tokens", maxTokens,
                "messages", List.of(
                        Map.of("role", "system", "content", system),
                        Map.of("role", "user", "content", userPrompt)));
        Flux<String> data = web.post()
                .uri("/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(body)
                .retrieve()
                .bodyToFlux(new ParameterizedTypeReference<ServerSentEvent<String>>() {})
                .mapNotNull(ServerSentEvent::data);
        return OpenAiCompatStream.decode(data, json, meters, model);
    }
}
