package com.example.positiondiff.search;

import java.util.ArrayList;
import java.util.List;

/**
 * Local tokenizer with no external dependencies.
 *
 * <p>Latin/digit runs are lower-cased and split on non-word characters.
 * CJK characters are indexed both as unigrams and adjacent bigrams <em>within
 * the same contiguous CJK run</em>, so both single-character and
 * two-character queries match Chinese text without a segmenter.
 */
public final class Tokenizer {

    private Tokenizer() {}

    public static boolean isCjk(int codePoint) {
        return (codePoint >= 0x4E00 && codePoint <= 0x9FFF)
                || (codePoint >= 0x3400 && codePoint <= 0x4DBF)
                || (codePoint >= 0xF900 && codePoint <= 0xFAFF);
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new ArrayList<>();
        String lower = text.toLowerCase();
        int n = lower.length();

        int i = 0;
        while (i < n) {
            int cp = lower.codePointAt(i);
            if (isCjk(cp)) {
                int runStart = i;
                StringBuilder run = new StringBuilder();
                do {
                    String ch = new String(Character.toChars(cp));
                    tokens.add(ch);
                    run.append(ch);
                    i += Character.charCount(cp);
                    if (i >= n) break;
                    cp = lower.codePointAt(i);
                } while (isCjk(cp));
                for (int b = 0; b + 1 < run.length(); b++) {
                    tokens.add(run.substring(b, b + 2));
                }
            } else if (Character.isLetterOrDigit(cp)) {
                int start = i;
                i += Character.charCount(cp);
                while (i < n && Character.isLetterOrDigit(lower.codePointAt(i))) {
                    i += Character.charCount(lower.codePointAt(i));
                }
                tokens.add(lower.substring(start, i));
            } else {
                i += Character.charCount(cp);
            }
        }
        return tokens;
    }
}
