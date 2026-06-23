package com.portfolio.recall.search;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;

class ReciprocalRankFusionTest {

    private static RetrievedChunk chunk(String id) {
        return new RetrievedChunk(id, "doc", 0, "content-" + id, "src", "en", 0.0);
    }

    @Test
    void itemsRankedHighInBothListsOutrankItemsRankedLowInBoth() {
        var a = chunk("a");
        var b = chunk("b");
        var c = chunk("c");
        List<RetrievedChunk> bm25 = List.of(a, b, c);
        List<RetrievedChunk> knn = List.of(b, a, c);

        var fused = ReciprocalRankFusion.fuse(bm25, knn, 60, 3);

        assertThat(fused).hasSize(3);
        // a and b are top-ranked in both lists; c is last in both → c must rank last.
        assertThat(fused.get(2).id()).isEqualTo("c");
        assertThat(fused).extracting(RetrievedChunk::id).containsExactlyInAnyOrder("a", "b", "c");
    }

    @Test
    void unionOfBothRankingsIsReturned() {
        var fused = ReciprocalRankFusion.fuse(List.of(chunk("a")), List.of(chunk("b")), 60, 10);
        assertThat(fused).extracting(RetrievedChunk::id).containsExactlyInAnyOrder("a", "b");
    }
}
