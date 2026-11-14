package com.portfolio.recall.llm;

/**
 * Why a generation stream ended, normalised across providers.
 *
 * <p>The reason matters because the text alone cannot tell a finished answer from one that
 * was cut: an answer that hit {@code max_tokens} streams, judges and reads exactly like a
 * complete one up to the point where it stops mid-sentence. Anything downstream that replays
 * an answer — the semantic cache above all — has to know which one it is holding.
 */
public enum FinishReason {
    /** The model ended its turn (Claude {@code end_turn}/{@code stop_sequence}, OpenAI {@code stop}). */
    COMPLETE,
    /** Cut at the output budget (Claude {@code max_tokens}, OpenAI {@code length}). */
    TRUNCATED,
    /** The provider declined or filtered the output (Claude {@code refusal}, OpenAI {@code content_filter}). */
    REFUSED,
    /** The provider did not say. Treated as complete, which is what the system assumed before it could ask. */
    UNREPORTED;

    /** Whether an answer that ended this way may be replayed verbatim to a later question. */
    public boolean cacheable() {
        return this == COMPLETE || this == UNREPORTED;
    }

    /** OpenAI-compatible {@code choices[0].finish_reason} (Groq, Ollama). */
    public static FinishReason fromOpenAi(String reason) {
        if (reason == null) return UNREPORTED;
        return switch (reason) {
            case "stop" -> COMPLETE;
            case "length" -> TRUNCATED;
            case "content_filter" -> REFUSED;
            default -> UNREPORTED;
        };
    }

    /** Anthropic {@code stop_reason}, as its wire string. */
    public static FinishReason fromAnthropic(String stopReason) {
        if (stopReason == null) return UNREPORTED;
        return switch (stopReason) {
            case "end_turn", "stop_sequence" -> COMPLETE;
            case "max_tokens", "model_context_window_exceeded" -> TRUNCATED;
            case "refusal" -> REFUSED;
            default -> UNREPORTED;
        };
    }
}
