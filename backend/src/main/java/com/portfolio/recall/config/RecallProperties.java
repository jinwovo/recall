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
        Rag rag,
        Storage storage) {

    public record Elasticsearch(String host, String index) {}

    public record Embedding(String serviceUrl, int dim) {}

    /**
     * Ingestion topic + failure policy: bounded exponential retries, then dead-letter
     * (docs/adr/0005). consumerConcurrency is listener threads, capped by partition count.
     */
    public record Kafka(String ingestionTopic, String ingestionDlqTopic,
                        int retryMaxAttempts, long retryBackoffMs,
                        int consumerConcurrency) {}

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

    /** RAG answer-quality guardrails (docs/adr/0004, 0009, 0013). */
    public record Rag(Judge judge, Sufficiency sufficiency, Conformal conformal) {
        /** Post-hoc groundedness judge: fail-open, bounded by timeoutSeconds. */
        public record Judge(boolean enabled, int timeoutSeconds) {}

        /**
         * Pre-generation sufficiency gate (docs/adr/0009): runs only when the reranker's
         * top score is below confidenceThreshold; fail-open, bounded by timeoutSeconds.
         *
         * <p>confidenceThreshold is a calibrated number, not a guess: eval/calibrate.py
         * chooses the most permissive value whose risk is provably bounded (docs/adr/0013),
         * and writes the certificate alongside it.
         */
        public record Sufficiency(boolean enabled, double confidenceThreshold, int timeoutSeconds) {}

        /**
         * Adaptive context sizing with a coverage guarantee (docs/adr/0013). {@code threshold}
         * is the conformal quantile produced by eval/calibrate.py for the configured
         * {@code alpha}; it is only meaningful together with the {@code temperature} it was
         * calibrated at. {@code maxK} is a hard context cap that overrides the certificate
         * when it binds. Disabled → retrieval.topK, unchanged.
         */
        /**
         * @param monitorEnabled  watch whether the deployed certificate still holds
         *                        (docs/adr/0016). Needs {@code enabled} too — monitoring a
         *                        certificate that was never issued is noise.
         * @param monitorAlpha    the monitor's own error budget, not the coverage level.
         *                        This is the chance the anytime-valid sequence ever excludes
         *                        the true miscoverage rate across the whole stream, so it
         *                        buys a false-alarm rate over the lifetime of the process
         *                        rather than per check.
         * @param monitorWarmup   judged answers to collect before the alarm can fire. The
         *                        sequence is valid from the first observation; this only
         *                        keeps a deploy from alarming off a handful of queries.
         */
        public record Conformal(boolean enabled, double alpha, double threshold,
                                double temperature, int maxK,
                                boolean monitorEnabled, double monitorAlpha,
                                int monitorWarmup) {}
    }

    /**
     * Raw-document archive (MinIO/S3). Documents larger than inlineMaxBytes travel through
     * Kafka as an objectKey reference instead of inline content — the claim-check pattern
     * (docs/adr/0005).
     */
    public record Storage(String endpoint, String accessKey, String secretKey,
                          String bucket, int inlineMaxBytes) {}
}
