package com.example.topk.engine;

import com.example.topk.model.Row;

import java.util.ArrayList;
import java.util.List;

/**
 * Mergeable per-group intermediate state for a top-k query.
 *
 * <p>A state keeps candidate rows in memory up to {@code budget}. Small groups
 * never exceed the budget and keep every row (full in-memory processing).
 * As soon as a group exceeds the budget, the state trims itself to its top-k
 * candidates only. Keeping the local top-k is always sufficient because the
 * global top-k of a union can only contain rows that belong to a local top-k.
 *
 * <p>{@link #merge} is commutative and associative: the row ordering is total
 * (see {@link RowOrdering}), so {@code merge(a,b)} and {@code merge(b,a)} hold
 * equivalent states, regardless of how many times trimming happened.
 */
public final class TopKState {

    private final int k;
    private final int budget;
    private final RowOrdering ordering;
    private final ArrayList<Row> rows = new ArrayList<>();
    private boolean bounded; // true once this state has trimmed to top-k

    public TopKState(int k, int budget, RowOrdering ordering) {
        if (k < 0) {
            throw new IllegalArgumentException("k must be >= 0, got " + k);
        }
        if (budget < k) {
            throw new IllegalArgumentException(
                    "groupBudget (" + budget + ") must be >= k (" + k + ")");
        }
        this.k = k;
        this.budget = budget;
        this.ordering = ordering;
    }

    public void add(Row r) {
        rows.add(r);
        trimIfOverBudget();
    }

    public void addAll(List<Row> more) {
        rows.addAll(more);
        trimIfOverBudget();
    }

    /** Returns a fresh state holding the union. Does not mutate either operand. */
    public TopKState merged(TopKState other) {
        checkCompatible(other);
        TopKState out = new TopKState(k, budget, ordering);
        out.rows.addAll(this.rows);
        out.rows.addAll(other.rows);
        out.bounded = this.bounded || other.bounded;
        out.trimIfOverBudget();
        return out;
    }

    /** Fold {@code other} into this state. */
    public void mergeInPlace(TopKState other) {
        checkCompatible(other);
        bounded = bounded || other.bounded;
        rows.addAll(other.rows);
        trimIfOverBudget();
    }

    private void checkCompatible(TopKState o) {
        if (o.k != k || o.budget != budget || o.ordering.descending() != ordering.descending()) {
            throw new IllegalArgumentException("cannot merge states built with different k/budget/order");
        }
    }

    private void trimIfOverBudget() {
        if (rows.size() > budget) {
            rows.sort(ordering);
            // Keep exactly the top-k: the minimal superset sufficient for any future merge.
            rows.subList(k, rows.size()).clear();
            bounded = true;
        }
    }

    public List<Row> topK() {
        ArrayList<Row> sorted = new ArrayList<>(rows);
        sorted.sort(ordering);
        return List.copyOf(sorted.subList(0, Math.min(k, sorted.size())));
    }

    public int retainedRows() {
        return rows.size();
    }

    public boolean bounded() {
        return bounded;
    }
}
