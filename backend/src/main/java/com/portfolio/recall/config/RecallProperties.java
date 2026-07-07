package com.portfolio.recall.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** Typed configuration bound from the {@code recall.*} block in application.yml. */
@ConfigurationProperties(prefix = "recall")
public record RecallProperties(
        Elasticsearch elasticsearch,
        Embedding embedding,
        Kafka kafka,
        SemanticCache semanticCache,
        Retrieval retrieval,
        Models models,
        Llm llm,
        Rag rag) {

    public record Elasticsearch(String host, String index) {}

    public record Embedding(String serviceUrl, int dim) {}

    public record Kafka(String ingestionTopic) {}

    public record SemanticCache(double threshold) {}

    /** candidates: fused size before rerank; topK: context passed to the LLM; rrfK: RRF constant. */
    public record Retrieval(int candidates, int topK, int rrfK) {}

    /** Model tiering (docs/adr/0002). 'primary' avoids the Java reserved word 'default'. */
    public record Models(String primary, String balanced, String cheap) {}

    /** LLM provider selection: claude (default) or groq (free, OpenAI-compatible). */
    public record Llm(String provider, Groq groq, Ollama ollama) {
        public record Groq(String baseUrl, String model) {}

        public record Ollama(String baseUrl, String model) {}
    }

    /** RAG answer-quality guardrails (docs/adr/0004). */
    public record Rag(Judge judge) {
        /** Post-hoc groundedness judge: fail-open, bounded by timeoutSeconds. */
        public record Judge(boolean enabled, int timeoutSeconds) {}
    }
}
