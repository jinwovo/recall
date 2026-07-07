package com.portfolio.recall.rag;

/**
 * Groundedness verdict for one generated answer (docs/adr/0004).
 * Serialized as-is onto the {@code groundedness} SSE event.
 */
public record Judgment(Verdict verdict, double score) {

    public enum Verdict {
        SUPPORTED(1.0),
        PARTIAL(0.5),
        UNSUPPORTED(0.0);

        private final double score;

        Verdict(double score) {
            this.score = score;
        }

        public Judgment toJudgment() {
            return new Judgment(this, score);
        }
    }
}
