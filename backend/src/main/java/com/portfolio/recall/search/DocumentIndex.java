package com.portfolio.recall.search;

import java.util.List;

/**
 * Abstraction over the search store. Keeping retrieval behind an interface isolates the
 * (verbose, version-sensitive) Elasticsearch client code from the orchestration in
 * {@link SearchService}, and makes the retrieval stages independently testable.
 */
public interface DocumentIndex {

    /** Create the index with the Nori + dense_vector mapping if it doesn't exist. */
    void ensureIndex();

    /** Lexical retrieval (BM25 over the Nori-analyzed content). */
    List<RetrievedChunk> bm25(String query, int size);

    /** Dense retrieval (kNN over the embedding vector). */
    List<RetrievedChunk> knn(float[] queryVector, int size);

    /** Idempotent upsert keyed by contentHash. */
    void upsert(ChunkDocument doc);
}
