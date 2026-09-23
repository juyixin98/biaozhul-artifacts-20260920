package com.example.uninorm;

import java.text.Normalizer;
import java.util.Locale;

/**
 * Text normalization for retrieval, with a full reverse offset map.
 *
 * <h2>Normalization policy (explicit)</h2>
 * <ol>
 *   <li><b>Unicode normalization form: NFKD</b> (compatibility decomposition),
 *       applied independently to every source code point. NFKD is used
 *       instead of NFC/NFKC because composition merges several source code
 *       points into one (the mapping is not injective), whereas decomposition
 *       expands one source code point into a sequence that never merges with a
 *       neighbouring source code point - so every normalized character can be
 *       attributed to exactly one original code point. As a retrieval bonus
 *       it folds compatibility characters: ligature {@code ﬁ -> fi},
 *       full-width {@code Ａ -> A}, roman numeral {@code Ⅳ -> IV},
 *       {@code ™ -> TM}, circled digits, square units, etc.
 *       <p>Note: per-code-point processing deliberately skips the canonical
 *       reordering step across code-point boundaries (canonical ordering of
 *       adjacent combining marks). Text and queries use the identical
 *       pipeline, so matching is unaffected; the result is a deterministic,
 *       idempotent (under this pipeline) key, not a canonical Unicode string.
 *       This is documented in the README.</li>
 *   <li><b>Case policy: locale-independent full/simple case folding</b>,
 *       implemented as {@code toUpperCase(ROOT)} then {@code toLowerCase(ROOT)}
 *       on the NFKD output. The ROOT locale makes folding deterministic and
 *       independent of the server locale. The upper-then-lower round trip
 *       captures one-to-many uppercase expansions, notably {@code ß -> SS
 *       -> ss} and similar ligatures that survived decomposition.
 *       Language-specific rules (Turkish {@code İ/i}, Lithuanian,
 *       final-sigma) are intentionally NOT applied; the trade-off is
 *       documented.</li>
 * </ol>
 *
 * The class is stateless and thread-safe.
 */
public final class TextNormalizer {

    private TextNormalizer() {
    }

    /**
     * Normalize {@code input} and build the reverse offset map.
     *
     * @throws IllegalArgumentException if input is null
     */
    public static NormalizedText normalize(String input) {
        if (input == null) {
            throw new IllegalArgumentException("input must not be null");
        }
        int cpCount = input.codePointCount(0, input.length());
        StringBuilder normalized = new StringBuilder(input.length());
        int[] cpUtf16Start = new int[cpCount + 1];
        int[] cpUtf8Start = new int[cpCount + 1];
        // One entry per UTF-16 char of the normalized output, pointing at the
        // original code point it came from. Grown dynamically because the
        // expansion length is not known up front.
        int[] normCharToCp = new int[input.length() == 0 ? 0 : input.length() * 2 + 8];
        int mapLen = 0;

        int utf16Offset = 0;
        int utf8Offset = 0;
        int cpIndex = 0;

        for (int pos = 0; pos < input.length(); ) {
            int cp = input.codePointAt(pos);
            int charCount = Character.charCount(cp);

            cpUtf16Start[cpIndex] = utf16Offset;
            cpUtf8Start[cpIndex] = utf8Offset;

            // 1) NFKD of this single code point (may expand: é -> e + 0x0301).
            String piece = new String(Character.toChars(cp));
            String decomposed = Normalizer.normalize(piece, Normalizer.Form.NFKD);
            // 2) Locale-independent case fold of the decomposed piece.
            String folded = foldCase(decomposed);

            for (int k = 0; k < folded.length(); k++) {
                if (mapLen == normCharToCp.length) {
                    normCharToCp = grow(normCharToCp);
                }
                normCharToCp[mapLen++] = cpIndex;
            }
            normalized.append(folded);

            utf16Offset += charCount;
            utf8Offset += utf8ByteLength(cp);
            pos += charCount;
            cpIndex++;
        }
        cpUtf16Start[cpCount] = utf16Offset;
        cpUtf8Start[cpCount] = utf8Offset;

        int[] trimmedMap = new int[mapLen];
        System.arraycopy(normCharToCp, 0, trimmedMap, 0, mapLen);
        return new NormalizedText(input, normalized.toString(), trimmedMap,
                cpUtf16Start, cpUtf8Start);
    }

    /**
     * Locale-independent case fold: ROOT-locale upper then lower, per code
     * point. The upper step captures expansions such as ß -> "SS".
     */
    static String foldCase(String s) {
        StringBuilder sb = new StringBuilder(s.length());
        for (int pos = 0; pos < s.length(); ) {
            int cp = s.codePointAt(pos);
            String piece = new String(Character.toChars(cp));
            String upper = piece.toUpperCase(Locale.ROOT);
            sb.append(upper.toLowerCase(Locale.ROOT));
            pos += Character.charCount(cp);
        }
        return sb.toString();
    }

    /** UTF-8 encoded length of one code point, in bytes. */
    static int utf8ByteLength(int cp) {
        if (cp < 0x80) {
            return 1;
        }
        if (cp < 0x800) {
            return 2;
        }
        if (cp < 0x10000) {
            return 3;
        }
        return 4;
    }

    private static int[] grow(int[] a) {
        int[] bigger = new int[a.length * 2];
        System.arraycopy(a, 0, bigger, 0, a.length);
        return bigger;
    }
}
