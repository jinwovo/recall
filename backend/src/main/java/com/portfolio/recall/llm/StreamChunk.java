package com.portfolio.recall.llm;

/** One element of a generation stream: text as it arrives, then at most one {@link End}. */
public sealed interface StreamChunk {

    record Text(String text) implements StreamChunk {}

    record End(FinishReason reason) implements StreamChunk {}
}
