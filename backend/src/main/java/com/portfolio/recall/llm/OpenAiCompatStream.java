package com.portfolio.recall.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import reactor.core.publisher.Flux;

/**
 * Decodes an OpenAI-compatible Chat Completions SSE stream (Groq, Ollama) into
 * {@link StreamChunk}s. Shared because the two clients had identical copies of the text
 * extraction and neither looked at {@code finish_reason} or usage.
 *
 * <p>Usage arrives on the final chunk when the provider sends it at all: as {@code usage}
 * (OpenAI, Ollama with {@code stream_options.include_usage}) or {@code x_groq.usage} (Groq).
 * A provider that sends none records nothing rather than a zero.
 */
final class OpenAiCompatStream {

    private OpenAiCompatStream() {}

    static Flux<StreamChunk> decode(Flux<String> data, ObjectMapper json,
                                    MeterRegistry meters, String model) {
        return Flux.defer(() -> {
            AtomicReference<FinishReason> finish = new AtomicReference<>(FinishReason.UNREPORTED);
            Flux<StreamChunk> text = data
                    .takeWhile(d -> !"[DONE]".equals(d))
                    .concatMapIterable(d -> parse(d, json, meters, model, finish));
            return Flux.concat(text, Flux.defer(() -> Flux.just(new StreamChunk.End(finish.get()))));
        });
    }

    private static List<StreamChunk> parse(String data, ObjectMapper json, MeterRegistry meters,
                                           String model, AtomicReference<FinishReason> finish) {
        JsonNode root;
        try {
            root = json.readTree(data);
        } catch (Exception e) {
            return List.of();
        }
        List<StreamChunk> out = new ArrayList<>(1);
        JsonNode choice = root.path("choices").path(0);
        JsonNode content = choice.path("delta").path("content");
        if (content.isTextual() && !content.asText().isEmpty()) {
            out.add(new StreamChunk.Text(content.asText()));
        }
        JsonNode reason = choice.path("finish_reason");
        if (reason.isTextual()) {
            finish.set(FinishReason.fromOpenAi(reason.asText()));
        }
        JsonNode usage = root.path("usage").isObject() ? root.path("usage") : root.path("x_groq").path("usage");
        if (usage.isObject()) {
            LlmUsage.record(meters, model,
                    usage.path("prompt_tokens").asLong(0),
                    usage.path("completion_tokens").asLong(0),
                    usage.path("prompt_tokens_details").path("cached_tokens").asLong(0));
        }
        return out;
    }
}
