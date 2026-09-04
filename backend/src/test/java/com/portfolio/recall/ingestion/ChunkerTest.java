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

    /** U+1F600 GRINNING FACE — one code point, two chars, so a char window can land inside it. */
    private static final String EMOJI = Character.toString(0x1F600);

    @Test
    void aSupplementaryCodePointIsNeverCutInHalf() {
        // The window is measured in chars and a char is not a character. Cutting between a
        // surrogate pair leaves an unpaired surrogate at the seam of both neighbouring
        // chunks — text that is no longer valid Unicode, and that then gets embedded,
        // indexed, and shown back to a reader.
        String text = EMOJI.repeat(50); // 100 chars, every boundary an odd offset away

        // codePoints() surfaces an unpaired surrogate as its own code point in D800-DFFF;
        // a whole pair arrives as U+1F600 and never lands in that range.
        for (String piece : chunker.chunk(text, 7, 2)) {
            assertThat(piece.codePoints().anyMatch(cp -> cp >= 0xD800 && cp <= 0xDFFF))
                    .as("a chunk carries an unpaired surrogate")
                    .isFalse();
        }
    }

    @Test
    void movingTheBoundaryDoesNotDropText() {
        // The seam is fixed by nudging the cut, so the obvious way to get it wrong is to
        // nudge past a code point and lose it.
        String text = "ab" + EMOJI + "cd" + Character.toString(0x1F601)
                + "ef" + Character.toString(0x1F602) + "gh";

        assertThat(String.join("", chunker.chunk(text, 5, 0)))
                .contains(EMOJI, Character.toString(0x1F601), Character.toString(0x1F602));
    }
}
