package com.example.uninorm;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.List;

/**
 * Text normalizer with original-offset mapping.
 *
 * Pipeline (documented in README):
 *   1. Split the input into segments: one starter code point plus all
 *      following combining marks (Mn / Mc / Me). A lone combining mark forms
 *      its own segment.
 *   2. Apply NFKD (compatibility decomposition + canonical reordering) to
 *      each segment. Because the whole combining sequence is normalized
 *      together, canonically equivalent sequences normalize identically.
 *      The output stays in DECOMPOSED form, so composed and decomposed
 *      spellings of e.g. "é" both become "e" + U+0301.
 *   3. Full case folding (see {@link CaseFolder}): ß → ss, ſ → s, ς → σ,
 *      root-locale lowercase for everything else.
 *
 * Every UTF-16 code unit of a normalized segment is mapped to the ORIGINAL
 * range of the whole segment. Consequence: a search hit always expands to the
 * complete original segment(s) it touches — it can never start or end in the
 * middle of a surrogate pair or a combining sequence.
 */
public final class TextNormalizer {

    private TextNormalizer() {}

    public static NormalizedText normalize(String input) {
        StringBuilder norm = new StringBuilder(input.length());
        List<Integer> starts = new ArrayList<>();
        List<Integer> ends = new ArrayList<>();

        int i = 0;
        final int n = input.length();
        while (i < n) {
            int segStart = i;
            int cp = input.codePointAt(i);
            i += Character.charCount(cp);
            // extend the segment through any following combining marks
            while (i < n) {
                int m = input.codePointAt(i);
                if (isCombiningMark(m)) {
                    i += Character.charCount(m);
                } else {
                    break;
                }
            }
            int segEnd = i;
            String segment = input.substring(segStart, segEnd);
            String normSeg = CaseFolder.fold(Normalizer.normalize(segment, Normalizer.Form.NFKD));
            for (int k = 0; k < normSeg.length(); k++) {
                starts.add(segStart);
                ends.add(segEnd);
            }
            norm.append(normSeg);
        }

        int[] s = new int[starts.size()];
        int[] e = new int[ends.size()];
        for (int k = 0; k < s.length; k++) {
            s[k] = starts.get(k);
            e[k] = ends.get(k);
        }
        return new NormalizedText(norm.toString(), s, e);
    }

    private static boolean isCombiningMark(int cp) {
        int t = Character.getType(cp);
        return t == Character.NON_SPACING_MARK
            || t == Character.COMBINING_SPACING_MARK
            || t == Character.ENCLOSING_MARK;
    }
}
