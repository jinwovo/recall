package com.portfolio.recall.llm;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.time.Duration;
import java.util.List;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;

class OpenAiCompatStreamTest {

    private final ObjectMapper json = new ObjectMapper();
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();

    private List<StreamChunk> decode(String... frames) {
        return OpenAiCompatStream.decode(Flux.just(frames), json, meters, "m")
                .collectList().block(Duration.ofSeconds(5));
    }

    private double tokens(String type) {
        return meters.counter("recall.llm.tokens", "model", "m", "type", type).count();
    }

    private static String delta(String text) {
        return "{\"choices\":[{\"delta\":{\"content\":\"" + text + "\"},\"finish_reason\":null}]}";
    }

    @Test
    void aGroqStreamCutAtTheLimitEndsTruncatedAndRecordsItsUsage() {
        List<StreamChunk> out = decode(
                delta("Hel"),
                delta("lo"),
                "{\"choices\":[{\"delta\":{},\"finish_reason\":\"length\"}],"
                        + "\"x_groq\":{\"usage\":{\"prompt_tokens\":120,\"completion_tokens\":7}}}",
                "[DONE]");

        assertThat(out).containsExactly(
                new StreamChunk.Text("Hel"),
                new StreamChunk.Text("lo"),
                new StreamChunk.End(FinishReason.TRUNCATED));
        assertThat(tokens("input")).isEqualTo(120.0);
        assertThat(tokens("output")).isEqualTo(7.0);
    }

    @Test
    void openAiStyleUsageOnATrailingChunkIsRecordedToo() {
        // Ollama with include_usage: finish_reason on one chunk, usage on a later empty one.
        decode(
                delta("ok"),
                "{\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}",
                "{\"choices\":[],\"usage\":{\"prompt_tokens\":50,\"completion_tokens\":2,"
                        + "\"prompt_tokens_details\":{\"cached_tokens\":40}}}",
                "[DONE]");

        assertThat(tokens("input")).isEqualTo(50.0);
        assertThat(tokens("cache_read")).isEqualTo(40.0);
    }

    @Test
    void aStreamThatNeverSaysWhyItStoppedEndsUnreportedAndRecordsNoUsage() {
        List<StreamChunk> out = decode(delta("a"), "not json", delta("b"));

        assertThat(out).containsExactly(
                new StreamChunk.Text("a"),
                new StreamChunk.Text("b"),
                new StreamChunk.End(FinishReason.UNREPORTED));
        assertThat(meters.find("recall.llm.tokens").counters()).isEmpty();
    }

    @Test
    void nothingAfterDoneIsRead() {
        List<StreamChunk> out = decode(delta("a"), "[DONE]", delta("late"));

        assertThat(out).containsExactly(
                new StreamChunk.Text("a"), new StreamChunk.End(FinishReason.UNREPORTED));
    }

    @Test
    void eachSubscriptionStartsFromAnUnreportedReason() {
        // The reason is per-stream state; a retry must not inherit the previous attempt's.
        Flux<StreamChunk> once = OpenAiCompatStream.decode(Flux.just(
                "{\"choices\":[{\"delta\":{\"content\":\"x\"},\"finish_reason\":\"length\"}]}"),
                json, meters, "m");
        once.blockLast();

        assertThat(OpenAiCompatStream.decode(Flux.just(delta("y")), json, meters, "m")
                .collectList().block()).last().isEqualTo(new StreamChunk.End(FinishReason.UNREPORTED));
    }

    @Test
    void providerReasonsMapOntoTheSharedVocabulary() {
        assertThat(FinishReason.fromOpenAi("stop")).isEqualTo(FinishReason.COMPLETE);
        assertThat(FinishReason.fromOpenAi("content_filter")).isEqualTo(FinishReason.REFUSED);
        assertThat(FinishReason.fromOpenAi("tool_calls")).isEqualTo(FinishReason.UNREPORTED);
        assertThat(FinishReason.fromAnthropic("end_turn")).isEqualTo(FinishReason.COMPLETE);
        assertThat(FinishReason.fromAnthropic("max_tokens")).isEqualTo(FinishReason.TRUNCATED);
        assertThat(FinishReason.fromAnthropic("refusal")).isEqualTo(FinishReason.REFUSED);
        assertThat(FinishReason.fromAnthropic(null)).isEqualTo(FinishReason.UNREPORTED);
    }
}
