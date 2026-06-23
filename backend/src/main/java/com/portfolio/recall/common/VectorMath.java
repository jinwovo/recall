package com.portfolio.recall.common;

/** Small vector helpers. Extracted so the similarity logic is unit-testable. */
public final class VectorMath {

    private VectorMath() {}

    /** Cosine similarity in [-1, 1]; returns -1 for null/length-mismatch/zero vectors. */
    public static double cosine(float[] a, float[] b) {
        if (a == null || b == null || a.length != b.length) {
            return -1;
        }
        double dot = 0, na = 0, nb = 0;
        for (int i = 0; i < a.length; i++) {
            dot += a[i] * b[i];
            na += a[i] * a[i];
            nb += b[i] * b[i];
        }
        return (na == 0 || nb == 0) ? -1 : dot / (Math.sqrt(na) * Math.sqrt(nb));
    }
}
