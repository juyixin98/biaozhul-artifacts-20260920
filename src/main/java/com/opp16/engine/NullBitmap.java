package com.opp16.engine;

import java.util.Arrays;

/**
 * Packed NULL bitmap: bit i is 1 when row i is non-null.
 *
 * Words are {@code long} (64 rows). A bitmap is deliberately its own object so
 * that "all-NULL column" is a first-class case every operator must survive.
 */
public final class NullBitmap {

    private final long[] words;
    private final int size;

    public NullBitmap(int size) {
        this.size = size;
        this.words = new long[wordCount(size)];
    }

    private NullBitmap(int size, long[] words) {
        this.size = size;
        this.words = words;
    }

    public static int wordCount(int size) {
        return (size + 63) >>> 6;
    }

    /** A bitmap where every row is non-null. */
    public static NullBitmap allPresent(int size) {
        NullBitmap bm = new NullBitmap(size);
        int fullWords = size >>> 6;
        Arrays.fill(bm.words, 0, fullWords, ~0L);
        int tail = size & 63;
        if (tail != 0) bm.words[fullWords] = (1L << tail) - 1L;
        return bm;
    }

    public int size() { return size; }

    public boolean isPresent(int row) {
        return (words[row >>> 6] & (1L << (row & 63))) != 0L;
    }

    public void setPresent(int row) {
        words[row >>> 6] |= (1L << (row & 63));
    }

    public void setNull(int row) {
        words[row >>> 6] &= ~(1L << (row & 63));
    }

    /** Count of non-null rows (used by count(col)). */
    public int countPresent() {
        int n = 0;
        for (int w = 0; w < words.length; w++) {
            long x = words[w];
            // mask off bits beyond size on the last word
            if (w == words.length - 1 && (size & 63) != 0) {
                x &= (1L << (size & 63)) - 1L;
            }
            n += Long.bitCount(x);
        }
        return n;
    }

    /**
     * Evaluate a batch range against the bitmap: returns a mask of positions
     * {@code [base, base+len)} that are non-null, one bit per *position*
     * (position 0 corresponds to row {@code base}).
     */
    public long presentMask(int base, int len) {
        int w0 = base >>> 6;
        int shift = base & 63;
        long x;
        if (shift == 0) {
            x = words[w0];
        } else {
            x = words[w0] >>> shift;
            int next = w0 + 1;
            if (next < words.length) x |= words[next] << (64 - shift);
        }
        long limitMask = len == 64 ? ~0L : (1L << len) - 1L;
        return x & limitMask;
    }

    long[] rawWords() { return words; }

    @Override
    public String toString() {
        StringBuilder sb = new StringBuilder(size);
        for (int i = 0; i < size; i++) sb.append(isPresent(i) ? '1' : '0');
        return sb.toString();
    }
}
