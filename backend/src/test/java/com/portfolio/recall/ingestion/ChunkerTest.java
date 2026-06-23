package com.portfolio.recall.ingestion;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;

class ChunkerTest {

    private final Chunker chunker = new Chunker();

    @Test
    void blankInputProducesNoChunks() {
        assertThat(chunker.chunk("")).isEmpty();
        assertThat(chunker.chunk("   ")).isEmpty();
    }

    @Test
    void shortTextIsASingleChunk() {
        assertThat(chunker.chunk("hello world")).containsExactly("hello world");
    }

    @Test
    void adjacentFullChunksShareTheOverlapWindow() {
        String text = "abcdefghij".repeat(300); // 3000 chars
        int size = 1000, overlap = 200;
        List<String> chunks = chunker.chunk(text, size, overlap);

        assertThat(chunks.size()).isGreaterThan(1);
        assertThat(chunks.get(0)).hasSize(size);
        String tailOfFirst = chunks.get(0).substring(size - overlap);
        String headOfSecond = chunks.get(1).substring(0, overlap);
        assertThat(tailOfFirst).isEqualTo(headOfSecond);
    }

    @Test
    void lastChunkReachesEndOfText() {
        String text = "abcdefghij".repeat(250); // 2500 chars, ends with "fghij"
        List<String> chunks = chunker.chunk(text, 1000, 200);
        assertThat(chunks.get(chunks.size() - 1)).endsWith("fghij");
    }
}
