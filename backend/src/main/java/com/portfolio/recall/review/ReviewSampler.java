package com.portfolio.recall.review;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.ReviewProperties;
import com.portfolio.recall.rag.ClaimReport;
import com.portfolio.recall.rag.Judgment;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.DoubleSupplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * Decides which judged answers a human sees (docs/adr/0018), and writes the ledger the
 * estimate is computed from.
 *
 * <p>The decision is a coin with bias pi, where pi depends only on the judge's verdict and the
 * configured design, both known before the coin is tossed. The label a human would give never
 * enters it. That is the condition for weighting each label by 1/pi to be unbiased, so the
 * decision is kept deliberately dumb: one lookup, one random draw.
 *
 * <p>Fire-and-forget and fail-open, like the query log: the reader is never waiting on the
 * ledger, and a failed write loses one row. Write failures do not depend on the answer's
 * label, so a lost row loses precision but does not bias the estimate.
 */
@Service
public class ReviewSampler {

    private static final Logger log = LoggerFactory.getLogger(ReviewSampler.class);

    private final ReviewItemRepository repo;
    private final ObjectMapper json;
    private final MeterRegistry meters;
    private final ReviewProperties props;
    private final DoubleSupplier coin;

    @Autowired
    public ReviewSampler(ReviewItemRepository repo, ObjectMapper json, MeterRegistry meters,
                         ReviewProperties props) {
        this(repo, json, meters, props, () -> ThreadLocalRandom.current().nextDouble());
    }

    ReviewSampler(ReviewItemRepository repo, ObjectMapper json, MeterRegistry meters,
                  ReviewProperties props, DoubleSupplier coin) {
        this.repo = repo;
        this.json = json;
        this.meters = meters;
        this.props = props == null ? ReviewProperties.DISABLED : props;
        this.coin = coin;
    }

    public boolean enabled() {
        return props.enabled();
    }

    public double pi(Judgment.Verdict verdict) {
        return switch (verdict) {
            case SUPPORTED -> props.piSupported();
            case PARTIAL -> props.piPartial();
            case UNSUPPORTED -> props.piUnsupported();
        };
    }

    /**
     * Toss this answer's coin and record the outcome. Call once per judged answer, with the
     * judge's verdict on the answer as generated (not on a filtered version of it), because
     * that is the prediction the label will correct.
     */
    public void offer(String query, List<RetrievedChunk> chunks, String answer, Judgment verdict,
                      Optional<ClaimReport> claims) {
        if (!enabled()) {
            return;
        }
        double pi = pi(verdict.verdict());
        boolean selected = coin.getAsDouble() < pi;
        ReviewItem item = selected
                ? ReviewItem.forReview(verdict.score(), verdict.verdict().name(), pi, query, answer,
                        toJson(passages(chunks)), claims.map(c -> toJson(claimRows(c, answer))).orElse(null))
                : ReviewItem.passedOver(verdict.score(), verdict.verdict().name(), pi);
        meters.counter("recall.review.items", "selected", String.valueOf(selected),
                "verdict", verdict.verdict().name()).increment();
        Mono.fromRunnable(() -> repo.save(item))
                .subscribeOn(Schedulers.boundedElastic())
                .subscribe(null, e -> log.warn("review ledger write failed: {}", e.getMessage()));
    }

    private static List<Map<String, Object>> passages(List<RetrievedChunk> chunks) {
        List<Map<String, Object>> out = new ArrayList<>(chunks.size());
        for (int i = 0; i < chunks.size(); i++) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("n", i + 1);
            row.put("source", chunks.get(i).source());
            row.put("content", chunks.get(i).content());
            out.add(row);
        }
        return out;
    }

    private static List<Map<String, Object>> claimRows(ClaimReport report, String answer) {
        List<Map<String, Object>> out = new ArrayList<>(report.claims().size());
        for (ClaimReport.Claim c : report.claims()) {
            if (c.start() < 0 || c.end() > answer.length() || c.start() >= c.end()) continue;
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("text", answer.substring(c.start(), c.end()));
            row.put("score", c.score());
            out.add(row);
        }
        return out;
    }

    private String toJson(Object o) {
        try {
            return json.writeValueAsString(o);
        } catch (Exception e) {
            return null;
        }
    }
}
