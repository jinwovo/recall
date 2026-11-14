package com.portfolio.recall.rag;

import java.util.List;

/**
 * The claim-level verdict on one answer (docs/adr/0017), serialized onto the {@code claims}
 * SSE event.
 *
 * <p>{@code certified} is true only when a calibrated threshold was applied <em>and</em> every
 * claim was scored. A claim past {@code max-claims}, or an uncalibrated threshold, means the
 * guarantee does not cover this answer, and the event says so rather than implying it.
 */
public record ClaimReport(double alpha, Double threshold, boolean certified, List<Claim> claims) {

    /** {@code score} is null for a claim that was not scored; such a claim is never withheld. */
    public record Claim(int start, int end, Double score, boolean kept) {}

    public long withheld() {
        return claims.stream().filter(c -> !c.kept()).count();
    }
}
