package com.example.uninorm;

/**
 * A normalized string plus, for every UTF-16 code unit of the normalized
 * string, the half-open range [origStart, origEnd) in the ORIGINAL string
 * (UTF-16 offsets) that produced it.
 *
 * Ranges are always aligned to code point boundaries of the original text and
 * to segment boundaries (a base character plus its combining marks is one
 * segment), so a mapped range never cuts a code point or a combining sequence.
 */
public final class NormalizedText {

    public final String text;
    /** origStart[i] / origEnd[i] describe normalized UTF-16 unit i. */
    public final int[] origStart;
    public final int[] origEnd;

    NormalizedText(String text, int[] origStart, int[] origEnd) {
        if (text.length() != origStart.length || text.length() != origEnd.length) {
            throw new IllegalArgumentException("mapping arrays must match normalized length");
        }
        this.text = text;
        this.origStart = origStart;
        this.origEnd = origEnd;
    }

    /**
     * Map a half-open normalized range [normStart, normEnd) back to the
     * half-open original range covering every source code point that
     * contributed to it. Returns {start, end} in original UTF-16 offsets.
     */
    public int[] toOriginalRange(int normStart, int normEnd) {
        if (normStart < 0 || normEnd > text.length() || normStart >= normEnd) {
            throw new IllegalArgumentException("bad normalized range [" + normStart + "," + normEnd + ")");
        }
        int start = Integer.MAX_VALUE;
        int end = Integer.MIN_VALUE;
        for (int i = normStart; i < normEnd; i++) {
            start = Math.min(start, origStart[i]);
            end = Math.max(end, origEnd[i]);
        }
        return new int[]{start, end};
    }
}
