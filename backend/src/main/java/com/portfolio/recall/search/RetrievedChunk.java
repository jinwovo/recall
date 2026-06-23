package com.portfolio.recall.search;

/** A chunk returned from retrieval, with a fused/rerank score. */
public record RetrievedChunk(
        String id,
        String docId,
        int chunkIndex,
        String content,
        String source,
        String lang,
        double score) {

    public RetrievedChunk withScore(double newScore) {
        return new RetrievedChunk(id, docId, chunkIndex, content, source, lang, newScore);
    }
}
