package com.portfolio.recall.llm;

import com.anthropic.client.AnthropicClient;
import com.anthropic.core.http.StreamResponse;
import com.anthropic.models.messages.CacheControlEphemeral;
import com.anthropic.models.messages.Message;
import com.anthropic.models.messages.MessageCountTokensParams;
import com.anthropic.models.messages.MessageCreateParams;
import com.anthropic.models.messages.RawMessageStreamEvent;
import com.anthropic.models.messages.TextBlockParam;
import com.portfolio.recall.config.RecallProperties;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;

/**
 * Thin wrapper over the Anthropic Java SDK with model tiering and prompt caching.
 *
 * <p>Cost levers (docs/adr/0002):
 * <ul>
 *   <li>tiering — pick the model via {@link ModelTier}</li>
 *   <li>prompt caching — stable system prompt carries a {@code cache_control} breakpoint;
 *       verify hits via {@code usage.cacheReadInputTokens()}</li>
 *   <li>token counting — {@link #countTokens} for pre-flight cost estimates</li>
 * </ul>
 */
@Component
public class ClaudeClient {

    private final AnthropicClient client;
    private final RecallProperties props;
    private final MeterRegistry meters;

    public ClaudeClient(AnthropicClient client, RecallProperties props, MeterRegistry meters) {
        this.client = client;
        this.props = props;
        this.meters = meters;
    }

    /** Stream the answer token-by-token for SSE. The SDK is blocking, so iterate on boundedElastic. */
    public Flux<String> streamAnswer(String system, String userPrompt, ModelTier tier) {
        String model = tier.modelId(props.models());
        MessageCreateParams params = baseParams(model, system, userPrompt);

        return Flux.<String>create(sink -> {
            try (StreamResponse<RawMessageStreamEvent> stream = client.messages().createStreaming(params)) {
                stream.stream()
                        .flatMap(event -> event.contentBlockDelta().stream())
                        .flatMap(delta -> delta.delta().text().stream())
                        .forEach(textDelta -> sink.next(textDelta.text()));
                sink.complete();
            } catch (Exception e) {
                sink.error(e);
            }
            // TODO: accumulate final message usage → record token/cost metrics for streaming too.
        }).subscribeOn(Schedulers.boundedElastic());
    }

    /** Non-streaming completion (used by the groundedness guard / classifier paths). */
    public String complete(String system, String userPrompt, ModelTier tier) {
        String model = tier.modelId(props.models());
        Message resp = client.messages().create(baseParams(model, system, userPrompt));
        StringBuilder sb = new StringBuilder();
        resp.content().forEach(block -> block.text().ifPresent(t -> sb.append(t.text())));
        recordUsage(model, resp);
        return sb.toString();
    }

    /** Pre-flight token count for cost estimation / max_tokens guarding. */
    public long countTokens(String text, ModelTier tier) {
        return client.messages()
                .countTokens(MessageCountTokensParams.builder()
                        .model(tier.modelId(props.models()))
                        .addUserMessage(text)
                        .build())
                .inputTokens();
    }

    private MessageCreateParams baseParams(String model, String system, String userPrompt) {
        // .model(String) is accepted; switch to Model.* constants if your SDK version requires it.
        return MessageCreateParams.builder()
                .model(model)
                .maxTokens(1024L)
                // Stable system prompt → prompt cache breakpoint (cache reads ≈ 0.1× input cost).
                .systemOfTextBlockParams(List.of(
                        TextBlockParam.builder()
                                .text(system)
                                .cacheControl(CacheControlEphemeral.builder().build())
                                .build()))
                .addUserMessage(userPrompt)
                .build();
    }

    private void recordUsage(String model, Message resp) {
        var usage = resp.usage();
        meters.counter("recall.llm.tokens", "model", model, "type", "output")
                .increment(usage.outputTokens());
        meters.counter("recall.llm.tokens", "model", model, "type", "input")
                .increment(usage.inputTokens());
        meters.counter("recall.llm.tokens", "model", model, "type", "cache_read")
                .increment(usage.cacheReadInputTokens().orElse(0L));
    }
}
