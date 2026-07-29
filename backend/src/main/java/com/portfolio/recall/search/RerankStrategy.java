package com.portfolio.recall.search;

/**
 * How the fused hybrid candidate set is re-ordered (docs/adr/0008). Exposed on
 * {@code /api/search?rerank=} so the eval harness can compare strategies head-to-head.
 *
 * <ul>
 *   <li>{@code CROSS_ENCODER} (default) — bge-reranker-v2-m3 scores each (query, passage)
 *       pair jointly; strongest signal, second model in memory.</li>
 *   <li>{@code M3} — bge-m3's own tri-modal self-hybrid: weighted dense + sparse lexical +
 *       ColBERT MaxSim, per the BGE-M3 paper; no extra model beyond the embedder.</li>
 * </ul>
 */
public enum RerankStrategy {
    CROSS_ENCODER,
    M3
}
