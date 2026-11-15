package com.portfolio.recall.review;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.after;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.config.ReviewProperties;
import com.portfolio.recall.rag.ClaimReport;
import com.portfolio.recall.rag.Judgment;
import com.portfolio.recall.search.RetrievedChunk;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

/**
 * The sampler is the one place the estimate's validity is decided (docs/adr/0018): pi must be
 * the configured value for the verdict, fixed before the coin, and written down with the row.
 */
class ReviewSamplerTest {

    private static final List<RetrievedChunk> CHUNKS = List.of(
            new RetrievedChunk("c1", "d1", 0, "Redis evicts with LRU.", "redis.md", "en", 0.9));
    private static final String ANSWER = "Redis evicts with LRU [1].";

    private final ReviewItemRepository repo = mock(ReviewItemRepository.class);
    private final SimpleMeterRegistry meters = new SimpleMeterRegistry();
    private final ReviewProperties design = new ReviewProperties(true, 0.02, 0.4, 0.1);

    private ReviewItem offered(double coin, Judgment.Verdict verdict, Optional<ClaimReport> claims) {
        new ReviewSampler(repo, new ObjectMapper(), meters, design, () -> coin)
                .offer("how does redis evict?", CHUNKS, ANSWER, verdict.toJudgment(), claims);
        ArgumentCaptor<ReviewItem> saved = ArgumentCaptor.forClass(ReviewItem.class);
        verify(repo, timeout(2000)).save(saved.capture());
        return saved.getValue();
    }

    @Test
    void piIsTheConfiguredValueForTheVerdict() {
        ReviewSampler sampler = new ReviewSampler(repo, new ObjectMapper(), meters, design, () -> 0.5);
        assertThat(sampler.pi(Judgment.Verdict.SUPPORTED)).isEqualTo(0.02);
        assertThat(sampler.pi(Judgment.Verdict.PARTIAL)).isEqualTo(0.4);
        assertThat(sampler.pi(Judgment.Verdict.UNSUPPORTED)).isEqualTo(0.1);
    }

    @Test
    void aSelectedAnswerCarriesWhatAReviewerNeedsAndTheOddsItHad() {
        ClaimReport report = new ClaimReport(0.1, null, false,
                List.of(new ClaimReport.Claim(0, ANSWER.length(), 0.83, true)));

        ReviewItem item = offered(0.39, Judgment.Verdict.PARTIAL, Optional.of(report));

        assertThat(item.isSelected()).isTrue();
        assertThat(item.getPi()).isEqualTo(0.4);
        assertThat(item.getPrediction()).isEqualTo(0.5);
        assertThat(item.getVerdict()).isEqualTo("PARTIAL");
        assertThat(item.getAnswer()).isEqualTo(ANSWER);
        assertThat(item.getPassages()).contains("\"n\":1").contains("redis.md");
        assertThat(item.getClaims()).contains("\"text\":\"" + ANSWER + "\"").contains("0.83");
        assertThat(item.getSupported()).isNull();
    }

    @Test
    void aPassedOverAnswerKeepsOnlyThePredictionAndTheOdds() {
        ReviewItem item = offered(0.41, Judgment.Verdict.PARTIAL, Optional.empty());

        assertThat(item.isSelected()).isFalse();
        assertThat(item.getPi()).isEqualTo(0.4);
        assertThat(item.getPrediction()).isEqualTo(0.5);
        assertThat(item.getAnswer()).isNull();
        assertThat(item.getPassages()).isNull();
        assertThat(meters.counter("recall.review.items", "selected", "false", "verdict", "PARTIAL")
                .count()).isEqualTo(1.0);
    }

    @Test
    void disabledWritesNothing() {
        new ReviewSampler(repo, new ObjectMapper(), meters, ReviewProperties.DISABLED, () -> 0.0)
                .offer("q", CHUNKS, ANSWER, Judgment.Verdict.SUPPORTED.toJudgment(), Optional.empty());
        verify(repo, after(200).never()).save(any());
    }

    @Test
    void aProbabilityOfZeroIsRefused() {
        // An answer that can never be selected can never be corrected, and its weight is 1/0.
        assertThatThrownBy(() -> new ReviewProperties(true, 0.0, 0.5, 0.5))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ReviewProperties(true, 0.1, 1.5, 0.5))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aFailedWriteDoesNotReachTheReader() {
        org.mockito.Mockito.when(repo.save(any())).thenThrow(new RuntimeException("db down"));
        new ReviewSampler(repo, new ObjectMapper(), meters, design, () -> 0.0)
                .offer("q", CHUNKS, ANSWER, Judgment.Verdict.SUPPORTED.toJudgment(), Optional.empty());
        verify(repo, timeout(2000)).save(any());
        verify(repo, never()).delete(any());
    }
}
