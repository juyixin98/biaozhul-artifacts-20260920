package com.example.topk.engine;

import com.example.topk.model.Row;

import java.util.Comparator;

/**
 * Total ordering of rows: first by value in the requested direction
 * (descending by default), ties always broken by the stable unique sequence
 * number ascending. Because this ordering is total (no two distinct rows are
 * equal), the top-k set and its row order are unique, so shard/merge order
 * cannot change the result.
 */
public final class RowOrdering implements Comparator<Row> {

    private final boolean descending;

    public RowOrdering(boolean descending) {
        this.descending = descending;
    }

    public boolean descending() {
        return descending;
    }

    public static RowOrdering of(String order) {
        return switch (order) {
            case "desc", "DESC" -> new RowOrdering(true);
            case "asc", "ASC" -> new RowOrdering(false);
            default -> throw new IllegalArgumentException("order must be 'asc' or 'desc', got: " + order);
        };
    }

    @Override
    public int compare(Row a, Row b) {
        int c = Long.compare(a.value(), b.value());
        if (descending) c = -c; // safe: Long.compare only returns -1/0/1
        if (c != 0) return c;
        return Long.compare(a.seq(), b.seq()); // stable unique tie-break, always ascending
    }
}
