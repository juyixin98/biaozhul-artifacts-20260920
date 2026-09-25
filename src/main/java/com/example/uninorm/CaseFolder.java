package com.example.uninorm;

import java.util.Locale;

/**
 * Full case folding (approximation of Unicode CaseFolding.txt, status C+F).
 *
 * Strategy: root-locale lowercase first (handles multi-char lowerings such as
 * U+0130 İ → "i̇"), then apply the special foldings that
 * {@link String#toLowerCase} does NOT perform, e.g. ß → ss (this is the
 * "case expansion" case covered by the acceptance tests).
 */
public final class CaseFolder {

    private CaseFolder() {}

    /** Fold a single code point; returns the folded string (may be longer than one char). */
    public static String foldCodePoint(int cp) {
        switch (cp) {
            case 0x00DF: // ß LATIN SMALL LETTER SHARP S
            case 0x1E9E: // ẞ LATIN CAPITAL LETTER SHARP S
                return "ss";
            case 0x017F: // ſ LATIN SMALL LETTER LONG S
                return "s";
            case 0x03C2: // ς GREEK SMALL LETTER FINAL SIGMA
                return "σ";
            case 0x1FBE: // ι GREEK PROSGEGRAMMENI
                return "ι";
            case 0x0345: // ͅ COMBINING GREEK YPOGEGRAMMENI
                return "ι";
            default:
                String s = new String(Character.toChars(cp));
                String lower = s.toLowerCase(Locale.ROOT);
                return lower;
        }
    }

    /** Fold a whole string, code point by code point. */
    public static String fold(String s) {
        StringBuilder out = new StringBuilder(s.length());
        int i = 0;
        while (i < s.length()) {
            int cp = s.codePointAt(i);
            out.append(foldCodePoint(cp));
            i += Character.charCount(cp);
        }
        return out.toString();
    }
}
