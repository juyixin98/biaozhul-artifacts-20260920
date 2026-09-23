package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.Arrays;

/**
 * Selection vector: a compact, ordered, repeatable list of row positions.
 *
 * <ul>
 *   <li>Sparse rows: a filter only appends survivors, so downstream operators
 *       touch matching rows instead of scanning the whole column.</li>
 *   <li>Repeated selection: the same subscript may appear many times; every
 *       occurrence is materialised (a projection/aggregation reads each one).</li>
 *   <li>Invalid subscripts: {@code rowCount} bounds-checks every append;
 *       a negative or out-of-range index raises {@link EngineException}.</li>
 * </ul>
 */
public final class SelectionVector {

    private int[] indices;
    private int count;
    private final int rowCount;

    public SelectionVector(int rowCount) {
        this(rowCount, 16);
    }

    public SelectionVector(int rowCount, int initialCapacity) {
        if (rowCount < 0) throw new EngineException("negative rowCount");
        this.rowCount = rowCount;
        this.indices = new int[Math.max(1, initialCapacity)];
        this.count = 0;
    }

    public int rowCount() { return rowCount; }
    public int size() { return count; }
    public boolean isEmpty() { return count == 0; }
    public int get(int i) {
        if (i < 0 || i >= count) throw new EngineException("selection position out of range: " + i);
        return indices[i];
    }

    private void grow() {
        if (count == indices.length) {
            indices = Arrays.copyOf(indices, Math.max(indices.length * 2, 16));
        }
    }

    /** Append one row subscript, validating {@code 0 <= row < rowCount}. */
    public void append(int row) {
        if (row < 0 || row >= rowCount) {
            throw new EngineException(
                    "invalid selection subscript " + row + " for table with " + rowCount + " rows"
                    + " (valid range: 0.." + (rowCount - 1) + ")");
        }
        grow();
        indices[count++] = row;
    }

    /**
     * Append every set bit of {@code mask} as a row position.
     * Bit {@code p} (0..len-1) maps to row {@code base+p}. This is how a
     * vectorised filter batch feeds its survivors into the selection vector.
     */
    public void appendMask(long mask, int base, int len) {
        while (mask != 0L) {
            int p = Long.numberOfTrailingZeros(mask);
            append(base + p); // bounds-checked
            mask &= mask - 1L;
        }
    }

    /**
     * Bulk-load externally supplied subscripts (the "selection" request field).
     * Every entry is validated - used to exercise duplicate and invalid-index
     * handling end to end.
     */
    public static SelectionVector fromIndices(int rowCount, int[] rows) {
        SelectionVector sv = new SelectionVector(rowCount, rows.length);
        for (int row : rows) sv.append(row);
        return sv;
    }

    public int[] toArray() { return Arrays.copyOf(indices, count); }

    public Json.Value toJson() {
        Json.Arr a = new Json.Arr();
        for (int i = 0; i < count; i++) a.add(indices[i]);
        return a;
    }

    @Override public String toString() {
        return Arrays.toString(toArray());
    }
}
