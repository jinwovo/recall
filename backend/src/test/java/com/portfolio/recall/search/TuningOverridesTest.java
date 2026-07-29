package com.portfolio.recall.search;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

class TuningOverridesTest {

    @Test
    void absentValuesFallBackToDefaults() {
        TuningOverrides none = TuningOverrides.of(null, null);
        assertThat(none.rrfKOr(60)).isEqualTo(60);
        assertThat(none.candidatesOr(50)).isEqualTo(50);
    }

    @Test
    void presentValuesOverrideDefaults() {
        TuningOverrides o = TuningOverrides.of(120, 25);
        assertThat(o.rrfKOr(60)).isEqualTo(120);
        assertThat(o.candidatesOr(50)).isEqualTo(25);
    }

    @Test
    void valuesAreClampedToSaneBounds() {
        TuningOverrides o = TuningOverrides.of(100_000, 1);
        assertThat(o.rrfKOr(60)).isEqualTo(500);       // rrf-k ceiling
        assertThat(o.candidatesOr(50)).isEqualTo(10);  // candidates floor
    }
}
