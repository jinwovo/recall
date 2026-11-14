package com.portfolio.recall.rag;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Every span has to be an exact slice of the answer — the UI marks withheld claims in place
 * by these offsets, so an off-by-one here strikes through the wrong words.
 */
class ClaimSplitterTest {

    private static List<String> slices(String answer) {
        return ClaimSplitter.split(answer).stream()
                .map(s -> answer.substring(s.start(), s.end())).toList();
    }

    private static List<String> scored(String answer) {
        return ClaimSplitter.split(answer).stream().map(ClaimSplitter.Span::scoringText).toList();
    }

    @Test
    void sentencesKeepTheirCitationsInTheSpanButNotInTheScoredText() {
        String a = "BM25 ranks by term frequency [1]. Dense vectors capture meaning [2][3]. RRF fuses both.";

        assertThat(slices(a)).containsExactly(
                "BM25 ranks by term frequency [1].",
                "Dense vectors capture meaning [2][3].",
                "RRF fuses both.");
        assertThat(scored(a)).containsExactly(
                "BM25 ranks by term frequency.",
                "Dense vectors capture meaning.",
                "RRF fuses both.");
    }

    @Test
    void aCitationAfterTheFullStopBelongsToThePrecedingSentence() {
        assertThat(slices("Kafka retries three times. [4] Then it dead-letters."))
                .containsExactly("Kafka retries three times. [4]", "Then it dead-letters.");
    }

    @Test
    void decimalsAndCommonAbbreviationsDoNotEndASentence() {
        assertThat(slices("Version 2.0 adds HyDE, e.g. for short queries. It costs one call."))
                .containsExactly("Version 2.0 adds HyDE, e.g. for short queries.", "It costs one call.");
    }

    @Test
    void listItemsAreClaimsAndTheirMarkersAreNotScored() {
        String a = "Two levers:\n- prompt caching cuts input cost\n- tiering routes to Haiku";

        assertThat(slices(a)).containsExactly(
                "Two levers:", "- prompt caching cuts input cost", "- tiering routes to Haiku");
        assertThat(scored(a).get(1)).isEqualTo("prompt caching cuts input cost");
    }

    @Test
    void fragmentsThatAssertNothingAreSkipped() {
        assertThat(slices("Sources:\n[1]\n1.\nThe cache is bounded.")).containsExactly("The cache is bounded.");
        assertThat(ClaimSplitter.split("   ")).isEmpty();
        assertThat(ClaimSplitter.split(null)).isEmpty();
    }

    @Test
    void anUnterminatedLastSentenceIsStillAClaim() {
        // A max_tokens cut ends mid-sentence; the fragment is a claim like any other.
        assertThat(slices("The gate is sequential. It stops when the verdict is"))
                .containsExactly("The gate is sequential.", "It stops when the verdict is");
    }
}
