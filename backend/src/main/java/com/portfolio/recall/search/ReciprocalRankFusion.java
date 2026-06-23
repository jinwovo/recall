package com.portfolio.recall.search;

import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Reciprocal Rank Fusion (docs/adr/0001): score(d) = Σ 1 / (k + rank_i(d)).
 * No score normalization needed — robust default for fusing BM25 + vector rankings.
 */
public final class ReciprocalRankFusion {

    private ReciprocalRankFusion() {}

    public static List<RetrievedChunk> fuse(List<List<RetrievedChunk>> rankings, int k, int size) {
        Map<String, Double> scores = new LinkedHashMap<>();
        Map<String, RetrievedChunk> byId = new LinkedHashMap<>();

        for (List<RetrievedChunk> ranking : rankings) {
            for (int rank = 0; rank < ranking.size(); rank++) {
                RetrievedChunk chunk = ranking.get(rank);
                scores.merge(chunk.id(), 1.0 / (k + rank + 1), Double::sum);
                byId.putIfAbsent(chunk.id(), chunk);
            }
        }

        return scores.entrySet().stream()
                .sorted(Map.Entry.<String, Double>comparingByValue().reversed())
                .limit(size)
                .map(e -> byId.get(e.getKey()).withScore(e.getValue()))
                .toList();
    }

    /** Convenience overload for the two-list (BM25 + kNN) case. */
    public static List<RetrievedChunk> fuse(
            List<RetrievedChunk> bm25, List<RetrievedChunk> knn, int k, int size) {
        return fuse(List.of(bm25, knn), k, size);
    }

    static Comparator<RetrievedChunk> byScoreDesc() {
        return Comparator.comparingDouble(RetrievedChunk::score).reversed();
    }
}
