package com.portfolio.recall.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * The active review design (docs/adr/0018): the probability that a judged answer is sent to
 * a human, by the judge's verdict.
 *
 * <p>These three numbers are the whole design, and they are fixed before any answer's coin is
 * tossed. That is what lets {@code python eval/active.py} weight each label by 1/pi and get
 * an unbiased estimate of the <em>true</em> groundedness rate, whatever the judge got wrong.
 * Start with all three equal (uniform sampling, which is prediction-powered inference); after
 * the first labelled period, {@code active.py --budget} prints the per-verdict values that
 * spend the same budget where the judge is actually wrong.
 *
 * <p>A probability of 0 is refused: an answer the design can never select is an answer the
 * estimate can never correct, and its weight 1/pi would be infinite.
 */
@ConfigurationProperties(prefix = "recall.review")
public record ReviewProperties(boolean enabled, double piSupported, double piPartial,
                               double piUnsupported) {

    public static final ReviewProperties DISABLED = new ReviewProperties(false, 0.05, 0.05, 0.05);

    public ReviewProperties {
        for (double p : new double[] {piSupported, piPartial, piUnsupported}) {
            if (!(p > 0.0 && p <= 1.0)) {
                throw new IllegalArgumentException(
                        "recall.review.pi-* must be in (0, 1], got " + p);
            }
        }
    }
}
