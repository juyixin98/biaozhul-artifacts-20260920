package com.example.segindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/** Splits text into lowercase alphanumeric terms. */
public final class Tokenizer {

    private static final Pattern SEPARATOR = Pattern.compile("[^\\p{L}\\p{N}]+");

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        String lower = text.toLowerCase(Locale.ROOT);
        List<String> terms = new ArrayList<>();
        for (String token : SEPARATOR.split(lower)) {
            if (!token.isEmpty()) {
                terms.add(token);
            }
        }
        return terms;
    }

    /** Normalizes a query term the same way document terms are normalized. */
    public static String normalizeTerm(String term) {
        return term.toLowerCase(Locale.ROOT).trim();
    }
}
