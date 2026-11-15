package com.portfolio.recall.review;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.server.ResponseStatusException;

class ReviewQueueTest {

    private static final String CLAIMS = "[{\"text\":\"Redis evicts with LRU.\",\"score\":0.83},"
            + "{\"text\":\"It is single-threaded.\",\"score\":null}]";

    private final ReviewItemRepository repo = mock(ReviewItemRepository.class);
    private final ObjectMapper json = new ObjectMapper().findAndRegisterModules();
    private final ReviewQueue queue = new ReviewQueue(repo, json);

    private static ReviewItem selected(long id) {
        ReviewItem item = ReviewItem.forReview(0.5, "PARTIAL", 0.4, "how?", "Redis evicts with LRU. "
                + "It is single-threaded.", "[{\"n\":1,\"source\":\"redis.md\",\"content\":\"LRU\"}]", CLAIMS);
        ReflectionTestUtils.setField(item, "id", id);
        return item;
    }

    private static ReviewItem passedOver(long id) {
        ReviewItem item = ReviewItem.passedOver(1.0, "SUPPORTED", 0.02);
        ReflectionTestUtils.setField(item, "id", id);
        return item;
    }

    @Test
    void theQueueIsBlindToEverythingAMachineSaid() throws Exception {
        when(repo.findBySelectedTrueAndSupportedIsNullOrderByIdAsc(any())).thenReturn(List.of(selected(7)));

        String body = json.writeValueAsString(queue.pending(10));

        assertThat(body).contains("Redis evicts with LRU.").contains("redis.md");
        // A reviewer who sees the judge's verdict or the claim score agrees with it by construction.
        assertThat(body).doesNotContain("PARTIAL").doesNotContain("0.83")
                .doesNotContain("score").doesNotContain("prediction");
    }

    @Test
    void aLabelIsRecordedOnceWithItsClaims() {
        ReviewItem item = selected(7);
        when(repo.findById(7L)).thenReturn(Optional.of(item));

        queue.label(7, new ReviewQueue.LabelRequest(false, List.of(true, false)));

        assertThat(item.getSupported()).isFalse();
        assertThat(item.getClaimLabels()).isEqualTo("[true,false]");
        verify(repo).save(item);
        assertThatThrownBy(() -> queue.label(7, new ReviewQueue.LabelRequest(true, null)))
                .isInstanceOfSatisfying(ResponseStatusException.class,
                        e -> assertThat(e.getStatusCode()).isEqualTo(HttpStatus.CONFLICT));
    }

    @Test
    void anAnswerTheCoinPassedOverCannotBeLabelled() {
        // It would enter the sample with a weight the design never gave it.
        when(repo.findById(3L)).thenReturn(Optional.of(passedOver(3)));

        assertThatThrownBy(() -> queue.label(3, new ReviewQueue.LabelRequest(true, null)))
                .isInstanceOfSatisfying(ResponseStatusException.class,
                        e -> assertThat(e.getStatusCode()).isEqualTo(HttpStatus.CONFLICT));
        verify(repo, never()).save(any());
    }

    @Test
    void claimLabelsMustMatchTheClaims() {
        when(repo.findById(7L)).thenReturn(Optional.of(selected(7)));

        assertThatThrownBy(() -> queue.label(7, new ReviewQueue.LabelRequest(true, List.of(true))))
                .isInstanceOfSatisfying(ResponseStatusException.class,
                        e -> assertThat(e.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST));
        assertThatThrownBy(() -> queue.label(7, new ReviewQueue.LabelRequest(true, Arrays.asList(true, null))))
                .isInstanceOf(ResponseStatusException.class);
        assertThatThrownBy(() -> queue.label(7, new ReviewQueue.LabelRequest(null, null)))
                .isInstanceOf(ResponseStatusException.class);
    }

    @Test
    void theExportStopsAtTheFirstUnlabelledSelection() throws Exception {
        ReviewItem labelled = selected(2);
        labelled.label(true, "[true,true]");
        ReviewItem pending = selected(4);
        when(repo.findFirstBySelectedTrueAndSupportedIsNullOrderByIdAsc()).thenReturn(Optional.of(pending));
        when(repo.findByIdLessThanOrderByIdAsc(4L)).thenReturn(List.of(passedOver(1), labelled, passedOver(3)));

        String[] lines = queue.export(false).strip().split("\n");

        assertThat(lines).hasSize(3);
        JsonNode first = json.readTree(lines[0]);
        assertThat(first.get("selected").asBoolean()).isFalse();
        assertThat(first.get("pi").asDouble()).isEqualTo(0.02);
        assertThat(first.has("supported")).isFalse();
        JsonNode second = json.readTree(lines[1]);
        assertThat(second.get("supported").asBoolean()).isTrue();
        assertThat(second.get("claims").get(0).get("score").asDouble()).isEqualTo(0.83);
        assertThat(second.get("claims").get(1).get("score").isNull()).isTrue();
        verify(repo, never()).findAllByOrderByIdAsc();
    }

    @Test
    void theSummaryCountsTheExportablePrefix() {
        when(repo.count()).thenReturn(10L);
        when(repo.countBySelectedTrue()).thenReturn(3L);
        when(repo.countBySelectedTrueAndSupportedIsNotNull()).thenReturn(1L);
        when(repo.findFirstBySelectedTrueAndSupportedIsNullOrderByIdAsc()).thenReturn(Optional.of(selected(6)));
        when(repo.countByIdLessThan(anyLong())).thenReturn(2L);

        ReviewQueue.Summary s = queue.summary();

        assertThat(s.pending()).isEqualTo(2);
        assertThat(s.exportable()).isEqualTo(2);
    }
}
