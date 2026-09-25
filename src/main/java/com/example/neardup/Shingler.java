package com.example.neardup;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Fixed shingling rules (part of the public, deterministic contract):
 *
 * <ol>
 *   <li>Normalize: lowercase (Locale.ROOT), split on any run of characters
 *       that are not ASCII letters or digits. Empty tokens are dropped.</li>
 *   <li>Shingles: word k-shingles — every window of k consecutive tokens,
 *       joined with U+0001. k is fixed by {@link NearDupConfig#shingleSize()}.</li>
 *   <li>Documents with fewer than k tokens produce an EMPTY shingle set.
 *       They can never be verified as near-duplicates (see
 *       {@link Jaccard#similarity}): this is a deliberate, documented
 *       limitation for very short documents.</li>
 * </ol>
 */
public final class Shingler {

    private static final Pattern NON_ALNUM = Pattern.compile("[^a-z0-9]+");
    static final String SHINGLE_SEPARATOR = "\u0001";

    private Shingler() {
    }

    public static List<String> tokenize(String text) {
        String[] parts = NON_ALNUM.split(text.toLowerCase(Locale.ROOT));
        List<String> tokens = new ArrayList<>(parts.length);
        for (String part : parts) {
            if (!part.isEmpty()) {
                tokens.add(part);
            }
        }
        return tokens;
    }

    public static Set<String> shingles(List<String> tokens, int k) {
        if (k < 1) {
            throw new IllegalArgumentException("shingle size must be >= 1, got " + k);
        }
        Set<String> result = new LinkedHashSet<>();
        if (tokens.size() < k) {
            return result;
        }
        for (int i = 0; i + k <= tokens.size(); i++) {
            result.add(String.join(SHINGLE_SEPARATOR, tokens.subList(i, i + k)));
        }
        return result;
    }
}
