package invidx.util;

import java.util.Arrays;

/** Minimal growable primitive long list (avoids boxing overhead in postings). */
public final class LongList {

    private long[] data;
    private int size;

    public LongList() {
        this(16);
    }

    public LongList(int initialCapacity) {
        this.data = new long[Math.max(2, initialCapacity)];
    }

    public void add(long v) {
        if (size == data.length) {
            data = Arrays.copyOf(data, data.length * 2);
        }
        data[size++] = v;
    }

    public int size() {
        return size;
    }

    public long get(int i) {
        return data[i];
    }

    public long[] toArray() {
        return Arrays.copyOf(data, size);
    }

    /** Internal backing array (may be larger than {@link #size()}); for in-place compaction. */
    public long[] getReferenceArray() {
        return data;
    }

    public void truncate(int newSize) {
        if (newSize < 0 || newSize > size) {
            throw new IllegalArgumentException("bad truncate size: " + newSize);
        }
        size = newSize;
    }
}
