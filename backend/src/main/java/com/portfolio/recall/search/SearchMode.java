package com.portfolio.recall.search;

/**
 * Retrieval mode. Exposed on {@code /api/search?mode=} so the eval harness can sweep
 * BM25-only vs vector-only vs hybrid and quantify the hybrid lift (docs/adr/0001).
 *
 * <p>{@code HYDE} retrieves with the embedding of an LLM-generated hypothetical answer
 * passage instead of the query embedding (docs/adr/0008) — experimental, not part of the
 * CI gate.
 */
public enum SearchMode {
    BM25,
    VECTOR,
    HYBRID,
    HYDE
}
