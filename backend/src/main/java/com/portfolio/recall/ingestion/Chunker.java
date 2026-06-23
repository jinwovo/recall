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
            int end = Math.min(t.length(), start + size);
            chunks.add(t.substring(start, end));
            if (end == t.length()) {
                break;
            }
        }
        return chunks;
    }
}
