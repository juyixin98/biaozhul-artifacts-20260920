package invidx.analyzer;

import java.text.BreakIterator;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Unicode-aware tokenizer: lower-cases word/digit runs.
 * Uses {@link BreakIterator} so the rule is identical for the indexer and
 * the full-scan reference implementation.
 */
public final class Tokenizer {

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        List<String> out = new ArrayList<>();
        BreakIterator bi = BreakIterator.getWordInstance(Locale.ROOT);
        bi.setText(text);
        int start = bi.first();
        for (int end = bi.next(); end != BreakIterator.DONE; start = end, end = bi.next()) {
            String word = text.substring(start, end);
            if (isAlphaOrDigit(word)) {
                out.add(word.toLowerCase(Locale.ROOT));
            }
        }
        return out;
    }

    private static boolean isAlphaOrDigit(String word) {
        for (int i = 0; i < word.length(); i++) {
            if (Character.isLetterOrDigit(word.charAt(i))) {
                return true;
            }
        }
        return false;
    }
}
