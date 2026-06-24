package com.portfolio.recall.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.cache.SemanticCacheService;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.persistence.QueryLogService;
import com.portfolio.recall.search.RetrievedChunk;
import com.portfolio.recall.search.SearchService;
import java.util.List;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Flux;

/**
 * RAG QA (docs/adr/0001, 0002): retrieve → assemble grounded prompt → stream answer over SSE,
 * with a semantic-cache short-circuit and a per-question query log.
 * SSE event types: {@code sources}, {@code token}, {@code cache}, {@code done}, {@code error}.
 */
@Service
public class RagService {

    /** Stable system prompt → carries the prompt-cache breakpoint in {@link ClaudeClient}. */
    private static final String SYSTEM_PROMPT = """
            You are Recall, a precise technical assistant. Answer ONLY from the provided
            context passages. Cite sources inline as [n] using the passage numbers.
            If the context does not contain the answer, say exactly: "I don't know based on
            the available documents." Do not invent facts or cite passages you did not use.
            """;

    /** Canned answer when retrieval returns no context — the no-hallucination floor. */
    private static final String IDK = "I don't know based on the available documents.";

    private final SearchService search;
    private final EmbeddingClient embeddings;
    private final SemanticCacheService cache;
    private final LlmClient llm;
    private final QueryLogService queryLog;
    private final ObjectMapper json;
    private final int topK;

    public RagService(SearchService search, EmbeddingClient embeddings, SemanticCacheService cache,
                      LlmClient llm, QueryLogService queryLog, ObjectMapper json,
                      RecallProperties props) {
        this.search = search;
        this.embeddings = embeddings;
        this.cache = cache;
        this.llm = llm;
        this.queryLog = queryLog;
        this.json = json;
        this.topK = props.retrieval().topK();
    }

    public Flux<ServerSentEvent<String>> ask(String query) {
        return Flux.defer(() -> {
            long start = System.nanoTime();
            // Embed once: the vector is the semantic-cache key AND is reused by hybridWithVector().
            return embeddings.embedOne(query)
                    .flatMapMany(vector -> cache.lookup(vector).flatMapMany(hit -> hit
                            .map(answer -> cached(query, answer, start))
                            .orElseGet(() -> generate(query, vector, start))));
        }).onErrorResume(e -> Flux.just(sse("error", e.getMessage())));
    }

    private Flux<ServerSentEvent<String>> cached(String query, String answer, long start) {
        Flux<ServerSentEvent<String>> head = Flux.just(sse("cache", "hit"), sse("token", toJson(answer)));
        Flux<ServerSentEvent<String>> tail = Flux.defer(() ->
                queryLog.record(query, "ask", true, 0, answer.length(), elapsedMs(start))
                        .thenMany(Flux.just(sse("done", ""))));
        return Flux.concat(head, tail);
    }

    private Flux<ServerSentEvent<String>> generate(String query, float[] vector, long start) {
        // Reuse the embedding from the cache key — no second embed call.
        return search.hybridWithVector(query, vector).flatMapMany(all -> {
            // Search returns the full reranked list; keep only the top-K as LLM context.
            List<RetrievedChunk> chunks = all.stream().limit(topK).toList();

            // Groundedness guard: no context → don't call the LLM, return the canned "I don't know".
            if (chunks.isEmpty()) {
                return Flux.concat(
                        Flux.just(sse("sources", "[]"), sse("token", toJson(IDK))),
                        Flux.defer(() -> queryLog.record(query, "ask", false, 0, IDK.length(), elapsedMs(start))
                                .thenMany(Flux.just(sse("done", "")))));
            }

            String prompt = buildPrompt(query, chunks);
            StringBuilder answer = new StringBuilder();

            Flux<ServerSentEvent<String>> sources = Flux.just(sse("sources", toJson(chunks)));
            Flux<ServerSentEvent<String>> tokens = llm
                    .streamAnswer(SYSTEM_PROMPT, prompt, ModelTier.PRIMARY)
                    .doOnNext(answer::append)
                    .map(t -> sse("token", toJson(t)));
            // TODO: post-hoc LLM-judge groundedness check; switch [n] citations → Claude native citations.
            Flux<ServerSentEvent<String>> done = Flux.defer(() ->
                    cache.put(vector, answer.toString(), query)
                            .then(queryLog.record(query, "ask", false, chunks.size(),
                                    answer.length(), elapsedMs(start)))
                            .thenMany(Flux.just(sse("done", ""))));

            return Flux.concat(sources, tokens, done);
        });
    }

    private static long elapsedMs(long startNanos) {
        return (System.nanoTime() - startNanos) / 1_000_000;
    }

    private String buildPrompt(String query, List<RetrievedChunk> chunks) {
        StringBuilder sb = new StringBuilder("Context passages:\n\n");
        for (int i = 0; i < chunks.size(); i++) {
            sb.append('[').append(i + 1).append("] (").append(chunks.get(i).source()).append(")\n")
                    .append(chunks.get(i).content()).append("\n\n");
        }
        sb.append("Question: ").append(query).append('\n');
        return sb.toString();
    }

    private String toJson(Object o) {
        try {
            return json.writeValueAsString(o);
        } catch (Exception e) {
            return "[]";
        }
    }

    private ServerSentEvent<String> sse(String event, String data) {
        return ServerSentEvent.<String>builder().event(event).data(data).build();
    }
}
