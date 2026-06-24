package com.portfolio.recall.llm;

import reactor.core.publisher.Flux;

/**
 * LLM provider abstraction so the RAG path can run on Claude (default) or a free/local
 * provider (Groq OpenAI-compatible) selected via {@code recall.llm.provider}.
 */
public interface LlmClient {

    /** Stream the answer token-by-token for SSE. */
    Flux<String> streamAnswer(String system, String userPrompt, ModelTier tier);
}
