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
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
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
@ConditionalOnProperty(name = "recall.llm.provider", havingValue = "claude", matchIfMissing = true)
public class ClaudeClient implements LlmClient {

    private final AnthropicClient client;
    private final RecallProperties props;
    private final MeterRegistry meters;
    private final long maxTokens;

    public ClaudeClient(AnthropicClient client, RecallProperties props, MeterRegistry meters) {
        this.client = client;
        this.props = props;
        this.meters = meters;
        this.maxTokens = props.llm().maxTokens();
    }

    /**
     * Stream the answer token-by-token for SSE. The SDK is blocking, so iterate on boundedElastic.
     *
     * <p>The text deltas are only part of the stream. {@code message_start} carries the input
     * and cache-read token counts, the final {@code message_delta} the output count and the
     * stop reason. Reading only the deltas left the answer path — the one that spends most of
     * the tokens — invisible to the cost panel, and made a {@code max_tokens} cut
     * indistinguishable from a finished answer.
     */
    @Override
    public Flux<StreamChunk> stream(String system, String userPrompt, ModelTier tier) {
        String model = tier.modelId(props.models());
        MessageCreateParams params = baseParams(model, system, userPrompt);

        return Flux.<StreamChunk>create(sink -> {
            long[] usage = new long[3]; // input, output, cache_read — billed even if the stream fails
            String[] stopReason = {null};
            try (StreamResponse<RawMessageStreamEvent> stream = client.messages().createStreaming(params)) {
                stream.stream().forEach(event -> {
                    event.messageStart().ifPresent(start -> {
                        var u = start.message().usage();
                        usage[0] = u.inputTokens();
                        usage[2] = u.cacheReadInputTokens().orElse(0L);
                    });
                    event.contentBlockDelta()
                            .flatMap(delta -> delta.delta().text())
                            .ifPresent(text -> sink.next(new StreamChunk.Text(text.text())));
                    event.messageDelta().ifPresent(delta -> {
                        // Cumulative, so the last one wins.
                        usage[1] = delta.usage().outputTokens();
                        delta.delta().stopReason().ifPresent(r -> stopReason[0] = r.asString());
                    });
                });
                sink.next(new StreamChunk.End(FinishReason.fromAnthropic(stopReason[0])));
                sink.complete();
            } catch (Exception e) {
                sink.error(e);
            } finally {
                LlmUsage.record(meters, model, usage[0], usage[1], usage[2]);
            }
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
                // Configurable: max_tokens is a hard cut (recall.llm.max-tokens). stream()
                // reports it as FinishReason.TRUNCATED, which keeps the answer out of the cache.
                .maxTokens(maxTokens)
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
        LlmUsage.record(meters, model, usage.inputTokens(), usage.outputTokens(),
                usage.cacheReadInputTokens().orElse(0L));
    }
}
