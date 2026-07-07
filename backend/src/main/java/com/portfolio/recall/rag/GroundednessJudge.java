package com.portfolio.recall.rag;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;

/**
 * Post-hoc groundedness judge (docs/adr/0004): after the answer streams out, a CHEAP-tier
 * LLM grades whether the answer's claims are backed by the retrieved passages.
 *
 * <p>Fail-open by design — the judge annotates answers, it never blocks or breaks them.
 * Any judge error/timeout/unparseable verdict yields {@link Optional#empty()}.
 */
@Service
public class GroundednessJudge {

    private static final Logger log = LoggerFactory.getLogger(GroundednessJudge.class);

    /** Single-word contract — robust to parse even from small local judge models. */
    private static final String JUDGE_SYSTEM = """
            You are a strict grader checking an answer against its source passages.
            Decide whether every factual claim in the answer is supported by the passages.
            Reply with exactly one word and nothing else:
            SUPPORTED - every claim in the answer is backed by the passages
            PARTIAL - some claims are backed by the passages, others are not
            UNSUPPORTED - the answer's claims are not backed by the passages
            """;

    private final LlmClient llm;
    private final MeterRegistry meters;
    private final boolean enabled;
    private final Duration timeout;

    public GroundednessJudge(LlmClient llm, MeterRegistry meters, RecallProperties props) {
        this.llm = llm;
        this.meters = meters;
        this.enabled = props.rag().judge().enabled();
        this.timeout = Duration.ofSeconds(props.rag().judge().timeoutSeconds());
    }

    public boolean enabled() {
        return enabled;
    }

    public Mono<Optional<Judgment>> judge(String question, List<RetrievedChunk> chunks, String answer) {
        if (!enabled) {
            return Mono.just(Optional.empty());
        }
        return llm.streamAnswer(JUDGE_SYSTEM, buildPrompt(question, chunks, answer), ModelTier.CHEAP)
                .collect(StringBuilder::new, StringBuilder::append)
                .map(sb -> parse(sb.toString()))
                .doOnNext(j -> j.ifPresent(this::recordMetrics))
                .timeout(timeout)
                .onErrorResume(e -> {
                    log.warn("groundedness judge skipped: {}", e.toString());
                    return Mono.just(Optional.empty());
                });
    }

    /**
     * Lenient parse: scan for the verdict keyword. UNSUPPORTED is checked first because
     * SUPPORTED is a substring of it.
     */
    static Optional<Judgment> parse(String raw) {
        String s = raw == null ? "" : raw.toUpperCase(Locale.ROOT);
        if (s.contains("UNSUPPORTED")) {
            return Optional.of(Judgment.Verdict.UNSUPPORTED.toJudgment());
        }
        if (s.contains("PARTIAL")) {
            return Optional.of(Judgment.Verdict.PARTIAL.toJudgment());
        }
        if (s.contains("SUPPORTED")) {
            return Optional.of(Judgment.Verdict.SUPPORTED.toJudgment());
        }
        return Optional.empty();
    }

    private void recordMetrics(Judgment j) {
        meters.summary("recall.rag.groundedness").record(j.score());
        meters.counter("recall.rag.judge.verdicts",
                "verdict", j.verdict().name().toLowerCase(Locale.ROOT)).increment();
    }

    private String buildPrompt(String question, List<RetrievedChunk> chunks, String answer) {
        StringBuilder sb = new StringBuilder("Source passages:\n\n");
        for (int i = 0; i < chunks.size(); i++) {
            sb.append('[').append(i + 1).append("]\n").append(chunks.get(i).content()).append("\n\n");
        }
        sb.append("Question: ").append(question).append("\n\n")
                .append("Answer to grade:\n").append(answer).append('\n');
        return sb.toString();
    }
}
