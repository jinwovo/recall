package com.portfolio.recall.search;

/**
 * Retrieval mode. Exposed on {@code /api/search?mode=} so the eval harness can sweep
 * BM25-only vs vector-only vs hybrid and quantify the hybrid lift (docs/adr/0001).
 */
public enum SearchMode {
    BM25,
    VECTOR,
    HYBRID
}
