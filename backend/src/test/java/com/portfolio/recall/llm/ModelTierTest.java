package com.portfolio.recall.llm;

import static org.assertj.core.api.Assertions.assertThat;

import com.portfolio.recall.config.RecallProperties;
import org.junit.jupiter.api.Test;

class ModelTierTest {

    private final RecallProperties.Models models =
            new RecallProperties.Models("opus", "sonnet", "haiku");

    @Test
    void eachTierMapsToItsConfiguredModelId() {
        assertThat(ModelTier.PRIMARY.modelId(models)).isEqualTo("opus");
        assertThat(ModelTier.BALANCED.modelId(models)).isEqualTo("sonnet");
        assertThat(ModelTier.CHEAP.modelId(models)).isEqualTo("haiku");
    }
}
