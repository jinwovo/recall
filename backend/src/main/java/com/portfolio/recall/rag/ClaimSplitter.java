package com.portfolio.recall.rag;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Splits a finished answer into the claims the factuality filter scores (docs/adr/0017).
 *
 * <p>A sentence is the first cut at an atomic claim, and a deliberate one: it needs no LLM
 * call, it is deterministic, and every claim is an exact span of the text the reader already
 * has, so the UI can mark it in place. A compound sentence is scored as one claim, which only
 * makes the filter stricter — its score is limited by its least-supported half.
 *
 * <p>Citation markers that follow a sentence's full stop ({@code "... works. [2]"}) belong to
 * that sentence, and are removed from the text that is scored: the cross-encoder should judge
 * whether the passage says this, not whether the answer says {@code [2]}.
 */
public final class ClaimSplitter {

    /** One claim: its span in the answer, and the text the scorer sees. */
    public record Span(int start, int end, String scoringText) {}

    private static final Pattern CITATION = Pattern.compile("\\s*\\[\\d+]");
    private static final Pattern TRAILING_CITATIONS = Pattern.compile("(?:\\s*\\[\\d+])+");
    private static final Pattern LIST_MARKER = Pattern.compile("^\\s*(?:[-*•]|\\d+[.)]|#+)\\s+");
    private static final Pattern WORD = Pattern.compile("\\p{L}{2,}");
    private static final Set<String> ABBREVIATIONS =
            Set.of("e.g", "i.e", "etc", "vs", "cf", "fig", "dr", "mr", "ms", "no", "approx");

    private ClaimSplitter() {}

    public static List<Span> split(String answer) {
        List<Span> out = new ArrayList<>();
        if (answer == null || answer.isBlank()) return out;
        int start = 0;
        int i = 0;
        int n = answer.length();
        while (i < n) {
            char c = answer.charAt(i);
            int end = -1;
            if (c == '\n') {
                end = i;
            } else if ((c == '.' || c == '!' || c == '?') && !isAbbreviation(answer, start, i)) {
                int j = i + 1;
                Matcher m = TRAILING_CITATIONS.matcher(answer).region(j, n);
                if (m.lookingAt()) j = m.end();
                if (j == n || Character.isWhitespace(answer.charAt(j))) {
                    end = j;
                    i = j - 1;
                }
            }
            if (end >= 0) {
                add(answer, start, end, out);
                start = end;
            }
            i++;
        }
        add(answer, start, n, out);
        return out;
    }

    private static boolean isAbbreviation(String s, int from, int dot) {
        int k = dot;
        while (k > from && (Character.isLetter(s.charAt(k - 1)) || s.charAt(k - 1) == '.')) k--;
        return ABBREVIATIONS.contains(s.substring(k, dot).toLowerCase());
    }

    private static void add(String answer, int from, int to, List<Span> out) {
        while (from < to && Character.isWhitespace(answer.charAt(from))) from++;
        while (to > from && Character.isWhitespace(answer.charAt(to - 1))) to--;
        if (from >= to) return;
        String text = CITATION.matcher(LIST_MARKER.matcher(answer.substring(from, to)).replaceFirst(""))
                .replaceAll("").trim();
        // Fragments with fewer than two words ("Sources:", "1.") assert nothing to check.
        Matcher words = WORD.matcher(text);
        int count = 0;
        while (count < 2 && words.find()) count++;
        if (count >= 2) out.add(new Span(from, to, text));
    }
}
