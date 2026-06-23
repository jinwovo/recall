package com.portfolio.recall.llm;

import com.portfolio.recall.config.RecallProperties;

/**
 * Model tiering (docs/adr/0002). Route each task to the cheapest model that does it well:
 * CHEAP (rewrite/classify) → BALANCED (routine) → PRIMARY (hard / final answers).
 */
public enum ModelTier {
    CHEAP,
    BALANCED,
    PRIMARY;

    public String modelId(RecallProperties.Models models) {
        return switch (this) {
            case CHEAP -> models.cheap();
            case BALANCED -> models.balanced();
            case PRIMARY -> models.primary();
        };
    }
}
