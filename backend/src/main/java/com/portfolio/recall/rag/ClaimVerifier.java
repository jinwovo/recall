package com.portfolio.recall.rag;

import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.embedding.EmbeddingClient.RerankItem;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * Claim-level factuality filter (docs/adr/0017).
 *
 * <p>Scores each claim of a finished answer by the best cross-encoder score it gets against
 * any passage that was sent to the LLM — "is this sentence entailed by something the model
 * was shown?" — and withholds the claims at or below the calibrated threshold. With the
 * threshold from {@code eval/factuality.py}, every claim left standing is supported with
 * probability at least 1 - alpha, under the exchangeability caveat ADR 0016 monitors.
 *
 * <p>The scorer is the reranker already in the stack: an entailment-style verifier, which the
 * 2026 robustness study (arXiv 2603.16817) finds as good as an LLM confidence scorer at a
 * hundredth of the cost. Fail-open, like the judge: a timeout or sidecar error leaves the
 * answer unfiltered and counted, never blocked.
 */
@Component
public class ClaimVerifier {

    private static final Logger log = LoggerFactory.getLogger(ClaimVerifier.class);
    /** Claims scored concurrently; each is one /rerank call over the sent passages. */
    private static final int CONCURRENCY = 4;

    private final EmbeddingClient embeddings;
    private final MeterRegistry meters;
    private final RecallProperties.Rag.Factuality config;

    public ClaimVerifier(EmbeddingClient embeddings, MeterRegistry meters, RecallProperties props) {
        this.embeddings = embeddings;
        this.meters = meters;
        this.config = props.rag().factuality();
    }

    public boolean enabled() {
        return config.enabled();
    }

    public Mono<Optional<ClaimReport>> verify(List<RetrievedChunk> chunks, String answer) {
        if (!config.enabled() || chunks.isEmpty()) return Mono.just(Optional.empty());
        List<ClaimSplitter.Span> spans = ClaimSplitter.split(answer);
        if (spans.isEmpty()) return Mono.just(Optional.empty());

        List<String> passages = chunks.stream().map(RetrievedChunk::content).toList();
        List<ClaimSplitter.Span> scored = spans.subList(0, Math.min(spans.size(), config.maxClaims()));

        return Flux.fromIterable(scored)
                .flatMapSequential(span -> embeddings
                        .rerank(span.scoringText(), passages, passages.size())
                        .map(ClaimVerifier::best), CONCURRENCY)
                .collectList()
                .map(scores -> Optional.of(report(spans, scores)))
                .timeout(Duration.ofSeconds(config.timeoutSeconds()))
                .onErrorResume(e -> {
                    log.warn("claim verification skipped (fail-open): {}", e.toString());
                    meters.counter("recall.rag.claims.skipped").increment();
                    return Mono.just(Optional.empty());
                });
    }

    private static double best(List<RerankItem> items) {
        return items.stream().mapToDouble(RerankItem::score).max().orElse(0.0);
    }

    private ClaimReport report(List<ClaimSplitter.Span> spans, List<Double> scores) {
        boolean calibrated = config.calibrated();
        List<ClaimReport.Claim> claims = new ArrayList<>(spans.size());
        for (int i = 0; i < spans.size(); i++) {
            ClaimSplitter.Span s = spans.get(i);
            Double score = i < scores.size() ? scores.get(i) : null;
            // Strictly greater, matching eval/factuality.py: a claim AT the threshold goes.
            boolean kept = score == null || !calibrated || score > config.threshold();
            claims.add(new ClaimReport.Claim(s.start(), s.end(), score, kept));
            String outcome = score == null ? "unscored" : kept ? "kept" : "withheld";
            meters.counter("recall.rag.claims", "outcome", outcome).increment();
        }
        boolean complete = scores.size() == spans.size();
        return new ClaimReport(config.alpha(), calibrated ? config.threshold() : null,
                calibrated && complete, claims);
    }
}
