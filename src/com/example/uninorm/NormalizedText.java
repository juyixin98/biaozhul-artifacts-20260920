package com.example.uninorm;

import java.util.Arrays;

/**
 * Result of {@link TextNormalizer#normalize(String)}: the normalized key plus
 * three aligned offset tables that map every normalized position back to the
 * exact original code point.
 *
 * <p>Tables are sorted strictly by construction (offsets are visited in
 * source order), so lookups are array index reads / binary searches.
 */
public final class NormalizedText {

    private final String original;
    private final String normalized;
    /** For normalized UTF-16 index i: original code-point index it came from. */
    private final int[] normCharToCp;
    /** Original code-point index k -> its start offset in original UTF-16. */
    private final int[] cpUtf16Start;
    /** Original code-point index k -> its start offset in original UTF-8. */
    private final int[] cpUtf8Start;

    NormalizedText(String original,
                   String normalized,
                   int[] normCharToCp,
                   int[] cpUtf16Start,
                   int[] cpUtf8Start) {
        this.original = original;
        this.normalized = normalized;
        this.normCharToCp = normCharToCp;
        this.cpUtf16Start = cpUtf16Start;
        this.cpUtf8Start = cpUtf8Start;
    }

    public String original() {
        return original;
    }

    public String normalized() {
        return normalized;
    }

    /** Number of code points in the original text. */
    public int originalCodePointCount() {
        return cpUtf16Start.length - 1;
    }

    /**
     * Map a half-open span {@code [normStart, normEnd)} of the normalized
     * string (UTF-16 indices) to the span of original code points it covers,
     * and return that original span in all three coordinate systems.
     *
     * @throws IllegalArgumentException if the span is empty, out of bounds, or
     *                                  does not align to the code-point grid
     *                                  of the normalized string
     */
    public OffsetRange mapRange(int normStart, int normEnd) {
        if (normStart < 0 || normEnd > normCharToCp.length
                || normStart >= normEnd) {
            throw new IllegalArgumentException(
                    "bad normalized span: [" + normStart + ", " + normEnd
                            + ") length=" + normCharToCp.length);
        }
        // start must not point at a low surrogate: it would cut a code point.
        if (Character.isLowSurrogate(normalized.charAt(normStart))) {
            throw new IllegalArgumentException(
                    "normalized start splits a code point (low surrogate)");
        }
        // end must not point at the second half of a surrogate pair; i.e. the
        // char just before end must not be a high surrogate.
        if (Character.isHighSurrogate(normalized.charAt(normEnd - 1))) {
            throw new IllegalArgumentException(
                    "normalized end splits a code point (high surrogate)");
        }

        int firstCp = normCharToCp[normStart];
        int lastCp = normCharToCp[normEnd - 1];
        return rangeOfCodePoints(firstCp, lastCp + 1);
    }

    /**
     * Build the {@link OffsetRange} covering original code points
     * {@code [firstCp, endCp)} ({@code endCp} exclusive). UTF-16/UTF-8
     * boundaries come straight from the precomputed code-point tables, so by
     * construction they sit on code-point boundaries.
     */
    private OffsetRange rangeOfCodePoints(int firstCp, int endCp) {
        int cpCount = originalCodePointCount();
        if (firstCp < 0 || endCp > cpCount || firstCp >= endCp) {
            throw new IllegalArgumentException(
                    "bad code-point span: [" + firstCp + ", " + endCp
                            + ") count=" + cpCount);
        }
        int startUtf16 = cpUtf16Start[firstCp];
        int endUtf16;
        int startUtf8 = cpUtf8Start[firstCp];
        int endUtf8;
        if (endCp == cpCount) {
            endUtf16 = original.length();
            endUtf8 = utf8ByteLength(original);
        } else {
            endUtf16 = cpUtf16Start[endCp];
            endUtf8 = cpUtf8Start[endCp];
        }
        return new OffsetRange(startUtf16, endUtf16, firstCp, endCp,
                startUtf8, endUtf8);
    }

    /** Total UTF-8 encoded length of the original string, in bytes. */
    public static int utf8ByteLength(String s) {
        int total = 0;
        for (int pos = 0; pos < s.length(); ) {
            int cp = s.codePointAt(pos);
            total += TextNormalizer.utf8ByteLength(cp);
            pos += Character.charCount(cp);
        }
        return total;
    }

    /**
     * Extract the original substring covered by a range, for verification and
     * display.
     */
    public String excerpt(OffsetRange r) {
        return original.substring(r.startUtf16(), r.endUtf16());
    }

    @Override
    public String toString() {
        return "NormalizedText{normalized=" + normalized
                + ", normCharToCp=" + Arrays.toString(normCharToCp) + "}";
    }
}
