package com.portfolio.recall.embedding;

import com.portfolio.recall.config.RecallProperties;
import java.util.List;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

/**
 * Client for the Python embedding/rerank sidecar (bge-m3 + bge-reranker-v2-m3).
 * See embedding-service/app/main.py for the matching API.
 */
@Component
public class EmbeddingClient {

    private final WebClient web;

    public EmbeddingClient(WebClient.Builder builder, RecallProperties props) {
        this.web = builder.baseUrl(props.embedding().serviceUrl()).build();
    }

    public Mono<List<float[]>> embed(List<String> texts) {
        return web.post().uri("/embed")
                .bodyValue(new EmbedRequest(texts))
                .retrieve()
                .bodyToMono(EmbedResponse.class)
                .map(EmbedResponse::embeddings);
    }

    public Mono<float[]> embedOne(String text) {
        return embed(List.of(text)).map(list -> list.get(0));
    }

    /** Cross-encoder rerank: returns indices+scores of the passages, best first. */
    public Mono<List<RerankItem>> rerank(String query, List<String> passages, int topK) {
        return web.post().uri("/rerank")
                .bodyValue(new RerankRequest(query, passages, topK))
                .retrieve()
                .bodyToMono(RerankResponse.class)
                .map(RerankResponse::results);
    }

    // ---- DTOs (match the FastAPI sidecar) ----
    public record EmbedRequest(List<String> texts) {}

    public record EmbedResponse(List<float[]> embeddings) {}

    public record RerankRequest(String query, List<String> passages, int topK) {}

    public record RerankResponse(List<RerankItem> results) {}

    public record RerankItem(int index, double score) {}
}
