package com.portfolio.recall.llm;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * The one place {@code recall.llm.tokens} is written, so every provider and both the streaming
 * and blocking paths feed the same series the cost panel reads.
 */
final class LlmUsage {

    private LlmUsage() {}

    static void record(MeterRegistry meters, String model, long input, long output, long cacheRead) {
        meters.counter("recall.llm.tokens", "model", model, "type", "input").increment(input);
        meters.counter("recall.llm.tokens", "model", model, "type", "output").increment(output);
        meters.counter("recall.llm.tokens", "model", model, "type", "cache_read").increment(cacheRead);
    }
}
