package com.portfolio.recall.review;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * One judged answer in the review ledger (docs/adr/0018).
 *
 * <p>Every judged answer gets a row, selected or not, because the estimator needs the judge's
 * prediction on all of them: the unselected rows are where the judge's opinion is used, the
 * selected ones are where it is corrected. Only a selected row keeps the text a reviewer needs.
 * {@code pi} is written when the coin is tossed and never changed. Recomputing it later, from
 * a config that has since moved, would silently void every estimate made from this row.
 */
@Entity
@Table(name = "review_item", indexes = @Index(name = "review_item_pending", columnList = "selected,supported"))
public class ReviewItem {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private Instant createdAt = Instant.now();

    /** The judge's score: 1.0 SUPPORTED, 0.5 PARTIAL, 0.0 UNSUPPORTED. */
    private double prediction;

    private String verdict;

    /** The probability this answer had of being selected, fixed before the coin. */
    private double pi;

    private boolean selected;

    @Column(length = 2000)
    private String query;

    @Column(columnDefinition = "text")
    private String answer;

    /** JSON: [{n, source, content}] — what the model was shown, in prompt order. */
    @Column(columnDefinition = "text")
    private String passages;

    /** JSON: [{text, score}] — the answer's claims as ClaimVerifier scored them; null if off. */
    @Column(columnDefinition = "text")
    private String claims;

    /** The human label: is the whole answer supported by the passages? Null until labelled. */
    private Boolean supported;

    /** JSON: [true, false, ...] aligned with {@link #claims}; null if claims were not labelled. */
    @Column(columnDefinition = "text")
    private String claimLabels;

    private Instant labelledAt;

    protected ReviewItem() {}

    private ReviewItem(double prediction, String verdict, double pi, boolean selected) {
        this.prediction = prediction;
        this.verdict = verdict;
        this.pi = pi;
        this.selected = selected;
    }

    /** A row the coin passed over: the judge's prediction and the odds, nothing else. */
    public static ReviewItem passedOver(double prediction, String verdict, double pi) {
        return new ReviewItem(prediction, verdict, pi, false);
    }

    /** A row for the queue, carrying everything a reviewer needs to label it blind. */
    public static ReviewItem forReview(double prediction, String verdict, double pi, String query,
                                       String answer, String passages, String claims) {
        ReviewItem item = new ReviewItem(prediction, verdict, pi, true);
        item.query = query;
        item.answer = answer;
        item.passages = passages;
        item.claims = claims;
        return item;
    }

    public void label(boolean supported, String claimLabels) {
        this.supported = supported;
        this.claimLabels = claimLabels;
        this.labelledAt = Instant.now();
    }

    public Long getId() {
        return id;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public double getPrediction() {
        return prediction;
    }

    public String getVerdict() {
        return verdict;
    }

    public double getPi() {
        return pi;
    }

    public boolean isSelected() {
        return selected;
    }

    public String getQuery() {
        return query;
    }

    public String getAnswer() {
        return answer;
    }

    public String getPassages() {
        return passages;
    }

    public String getClaims() {
        return claims;
    }

    public Boolean getSupported() {
        return supported;
    }

    public String getClaimLabels() {
        return claimLabels;
    }
}
