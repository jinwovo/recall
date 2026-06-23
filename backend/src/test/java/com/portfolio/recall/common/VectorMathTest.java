package com.portfolio.recall.common;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import org.junit.jupiter.api.Test;

class VectorMathTest {

    @Test
    void identicalVectorsCosineIsOne() {
        float[] v = {1, 2, 3};
        assertThat(VectorMath.cosine(v, v)).isCloseTo(1.0, within(1e-9));
    }

    @Test
    void orthogonalVectorsCosineIsZero() {
        assertThat(VectorMath.cosine(new float[] {1, 0}, new float[] {0, 1})).isCloseTo(0.0, within(1e-9));
    }

    @Test
    void lengthMismatchReturnsMinusOne() {
        assertThat(VectorMath.cosine(new float[] {1, 2}, new float[] {1, 2, 3})).isEqualTo(-1.0);
    }

    @Test
    void zeroVectorReturnsMinusOne() {
        assertThat(VectorMath.cosine(new float[] {0, 0}, new float[] {1, 1})).isEqualTo(-1.0);
    }
}
