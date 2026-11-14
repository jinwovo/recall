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

    /** The answer as the reader is left with it: every withheld span cut out. */
    public String keptText(String answer) {
        StringBuilder out = new StringBuilder(answer.length());
        int at = 0;
        for (Claim c : claims.stream().filter(c -> !c.kept())
                .sorted(java.util.Comparator.comparingInt(Claim::start)).toList()) {
            if (c.start() < at || c.end() > answer.length()) continue;   // defensive: never mis-slice
            out.append(answer, at, c.start());
            at = c.end();
        }
        return out.append(answer, Math.min(at, answer.length()), answer.length()).toString()
                .replaceAll("[ \t]{2,}", " ").strip();
    }
}
