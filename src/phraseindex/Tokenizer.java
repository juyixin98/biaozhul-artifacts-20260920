package phraseindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Fixed tokenization rule: split on whitespace and lowercase every token.
 *
 * Whitespace is {@link Character#isWhitespace(char)} (space, tab, newline,
 * carriage return and other Unicode whitespace). Runs of whitespace are one
 * separator; leading/trailing/duplicated whitespace produce no empty tokens,
 * so an empty or whitespace-only document tokenizes to an empty list.
 */
public final class Tokenizer {

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new ArrayList<>();
        if (text == null) {
            return tokens;
        }
        int n = text.length();
        int start = -1;
        for (int i = 0; i < n; i++) {
            if (Character.isWhitespace(text.charAt(i))) {
                if (start >= 0) {
                    tokens.add(text.substring(start, i).toLowerCase(Locale.ROOT));
                    start = -1;
                }
            } else if (start < 0) {
                start = i;
            }
        }
        if (start >= 0) {
            tokens.add(text.substring(start).toLowerCase(Locale.ROOT));
        }
        return tokens;
    }
}
