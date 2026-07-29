package com.portfolio.recall.search;

/**
 * Per-request overrides of retrieval knobs (docs/adr/0010): the self-tuning sweep probes
 * candidate configurations through the public API instead of redeploying per combination.
 * Absent values fall back to configured defaults; present values are clamped to sane
 * bounds so the tuning surface cannot be abused into a resource-exhaustion vector.
 */
public record TuningOverrides(Integer rrfK, Integer candidates) {

    public static final TuningOverrides NONE = new TuningOverrides(null, null);

    private static final int RRF_K_MIN = 1, RRF_K_MAX = 500;
    private static final int CANDIDATES_MIN = 10, CANDIDATES_MAX = 200;

    public static TuningOverrides of(Integer rrfK, Integer candidates) {
        return new TuningOverrides(
                clamp(rrfK, RRF_K_MIN, RRF_K_MAX),
                clamp(candidates, CANDIDATES_MIN, CANDIDATES_MAX));
    }

    public int rrfKOr(int defaultValue) {
        return rrfK != null ? rrfK : defaultValue;
    }

    public int candidatesOr(int defaultValue) {
        return candidates != null ? candidates : defaultValue;
    }

    private static Integer clamp(Integer value, int min, int max) {
        return value == null ? null : Math.max(min, Math.min(value, max));
    }
}
