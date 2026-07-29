package com.portfolio.recall.rag;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;

/**
 * Pre-generation sufficiency gate (docs/adr/0009, after "Sufficient Context", ICLR 2025):
 * before spending a PRIMARY-tier generation, decide whether the retrieved passages can
 * actually answer the question. Insufficient context → abstain early, which is both the
 * cheapest and the least hallucination-prone outcome.
 *
 * <p>Two-stage so the happy path stays fast: when the reranker's top score clears
 * {@code confidence-threshold} the check is skipped entirely (no added TTFT); only
 * low-confidence retrievals pay a CHEAP-tier autorater call. Fail-open — any error,
 * timeout or unparseable verdict allows generation; the post-hoc groundedness judge
 * (docs/adr/0004) still guards the output.
 */
@Service
public class SufficiencyCheck {

    private static final Logger log = LoggerFactory.getLogger(SufficiencyCheck.class);

    /**
     * The autorater sees only the best-ranked passages: if the answer is not in the top 4
     * after reranking, more tail passages will not make the context sufficient — and half
     * the prompt means half the prefill latency on small local models.
     */
    private static final int CONTEXT_PASSAGES = 4;

    /** Single-word contract — robust to parse even from small local models. */
    private static final String SYSTEM = """
            You are a retrieval quality checker. Given a question and context passages,
            decide whether the passages contain the information needed to answer the
            question. Reply with exactly one word and nothing else:
            SUFFICIENT - the passages contain the information needed to answer
            INSUFFICIENT - the passages do not contain the information needed
            """;

    private final LlmClient llm;
    private final MeterRegistry meters;
    private final boolean enabled;
    private final double confidenceThreshold;
    private final Duration timeout;

    public SufficiencyCheck(LlmClient llm, MeterRegistry meters, RecallProperties props) {
        this.llm = llm;
        this.meters = meters;
        this.enabled = props.rag().sufficiency().enabled();
        this.confidenceThreshold = props.rag().sufficiency().confidenceThreshold();
        this.timeout = Duration.ofSeconds(props.rag().sufficiency().timeoutSeconds());
    }

    /** {@code true} → proceed to generation; {@code false} → abstain before the PRIMARY call. */
    public Mono<Boolean> allowGeneration(String question, List<RetrievedChunk> chunks) {
        if (!enabled || chunks.isEmpty()) {
            return Mono.just(true);
        }
        // Reranker scores are normalized [0..1]; a confident top hit needs no second opinion.
        if (chunks.get(0).score() >= confidenceThreshold) {
            meters.counter("recall.rag.sufficiency.skips").increment();
            return Mono.just(true);
        }
        return llm.streamAnswer(SYSTEM, buildPrompt(question, chunks), ModelTier.CHEAP)
                .collect(StringBuilder::new, StringBuilder::append)
                .map(sb -> parse(sb.toString()))
                .doOnNext(sufficient -> meters.counter("recall.rag.sufficiency.verdicts",
                        "verdict", sufficient ? "sufficient" : "insufficient").increment())
                .timeout(timeout)
                .onErrorResume(e -> {
                    log.warn("sufficiency check skipped (fail-open): {}", e.toString());
                    return Mono.just(true);
                });
    }

    /**
     * Lenient parse; INSUFFICIENT is checked first because SUFFICIENT is a substring of it.
     * No verdict found → fail-open to generation.
     */
    static boolean parse(String raw) {
        String s = raw == null ? "" : raw.toUpperCase(Locale.ROOT);
        if (s.contains("INSUFFICIENT")) {
            return false;
        }
        return true;
    }

    private String buildPrompt(String question, List<RetrievedChunk> chunks) {
        List<RetrievedChunk> top = chunks.stream().limit(CONTEXT_PASSAGES).toList();
        StringBuilder sb = new StringBuilder("Context passages:\n\n");
        for (int i = 0; i < top.size(); i++) {
            sb.append('[').append(i + 1).append("]\n").append(top.get(i).content()).append("\n\n");
        }
        sb.append("Question: ").append(question).append('\n');
        return sb.toString();
    }
}
