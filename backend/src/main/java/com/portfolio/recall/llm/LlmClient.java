package com.portfolio.recall.llm;

import reactor.core.publisher.Flux;

/**
 * LLM provider abstraction so the RAG path can run on Claude (default) or a free/local
 * provider (Groq OpenAI-compatible) selected via {@code recall.llm.provider}.
 */
public interface LlmClient {

    /**
     * Stream the answer as text chunks, followed by exactly one {@link StreamChunk.End} saying
     * why it stopped. Implementations record token usage for the call as a side effect.
     */
    Flux<StreamChunk> stream(String system, String userPrompt, ModelTier tier);

    /** Text only, for callers that collect the whole output and do not replay it. */
    default Flux<String> streamAnswer(String system, String userPrompt, ModelTier tier) {
        return stream(system, userPrompt, tier)
                .ofType(StreamChunk.Text.class)
                .map(StreamChunk.Text::text);
    }
}
