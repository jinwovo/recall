package com.portfolio.recall.ingestion;

import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * Character-window chunker with overlap. Good enough for the scaffold; swap for a
 * token-aware / sentence-aware splitter (e.g. by the embedding model's tokenizer) later.
 */
@Component
public class Chunker {

    private static final int SIZE = 1200;
    private static final int OVERLAP = 200;

    public List<String> chunk(String text) {
        return chunk(text, SIZE, OVERLAP);
    }

    public List<String> chunk(String text, int size, int overlap) {
        List<String> chunks = new ArrayList<>();
        if (text == null || text.isBlank()) {
            return chunks;
        }
        String t = text.strip();
        int step = Math.max(1, size - overlap);
        for (int start = 0; start < t.length(); start += step) {
            int from = onCodePointBoundary(t, start);
            int end = onCodePointBoundary(t, Math.min(t.length(), start + size));
            if (end > from) {
                chunks.add(t.substring(from, end));
            }
            if (end == t.length()) {
                break;
            }
        }
        return chunks;
    }

    /**
     * Nudges an index off the middle of a surrogate pair.
     *
     * <p>The window is measured in {@code char}s, and a {@code char} is not a character: an
     * emoji or any other supplementary-plane code point is two of them. Cutting between the
     * pair leaves an unpaired surrogate at the seam of both neighbouring chunks — text that
     * is no longer valid Unicode, and that then goes on to be embedded, indexed and shown
     * back to a reader. Moving the boundary one {@code char} earlier keeps the pair whole in
     * whichever chunk it lands, and costs at most one {@code char} of the window.
     */
    private static int onCodePointBoundary(String t, int index) {
        return index > 0 && index < t.length() && Character.isLowSurrogate(t.charAt(index))
                ? index - 1
                : index;
    }
}
