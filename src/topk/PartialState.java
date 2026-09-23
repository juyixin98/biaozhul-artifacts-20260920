package topk;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Mergeable intermediate state of a grouped TopK computation.
 *
 * For each group we keep at most k rows, always sorted by the total rank
 * order (value DESC, seq ASC). Because the rank order is total, "top k of
 * the union" is associative and commutative, so partial states can be
 * merged in any order and always yield the same final result.
 */
public final class PartialState {

    private final int k;
    /** group -> top-k rows, sorted by rank order, size <= k. TreeMap keeps group output order stable. */
    private final TreeMap<String, List<Row>> groups = new TreeMap<>();

    public PartialState(int k) {
        if (k < 0) throw new IllegalArgumentException("k must be >= 0");
        this.k = k;
    }

    public int k() { return k; }

    /** Groups in alphabetical order, each a rank-ordered list of at most k rows. */
    public Map<String, List<Row>> groups() {
        TreeMap<String, List<Row>> copy = new TreeMap<>();
        for (Map.Entry<String, List<Row>> e : groups.entrySet()) {
            copy.put(e.getKey(), List.copyOf(e.getValue()));
        }
        return copy;
    }

    /** Insert one row into this state, keeping only the k best per group. */
    public void add(Row r) {
        if (k == 0) {
            // group exists in the input, so it appears in the result with an empty list
            groups.computeIfAbsent(r.group(), g -> new ArrayList<>());
            return;
        }
        List<Row> list = groups.computeIfAbsent(r.group(), g -> new ArrayList<>());
        if (list.size() == k && Row.RANK_ORDER.compare(r, list.get(list.size() - 1)) >= 0) {
            return; // not better than the current k-th row
        }
        int pos = 0;
        while (pos < list.size() && Row.RANK_ORDER.compare(list.get(pos), r) <= 0) pos++;
        list.add(pos, r);
        if (list.size() > k) list.remove(list.size() - 1);
    }

    /** Merge another partial state into this one. */
    public void mergeFrom(PartialState other) {
        if (other.k != this.k) {
            throw new IllegalArgumentException("cannot merge states with different k");
        }
        for (Map.Entry<String, List<Row>> e : other.groups.entrySet()) {
            List<Row> mine = groups.get(e.getKey());
            if (mine == null) {
                groups.put(e.getKey(), new ArrayList<>(e.getValue()));
            } else {
                groups.put(e.getKey(), mergeSorted(mine, e.getValue(), k));
            }
        }
    }

    /** Top-k of the union of two rank-sorted lists. */
    static List<Row> mergeSorted(List<Row> a, List<Row> b, int k) {
        List<Row> out = new ArrayList<>(Math.min(k, a.size() + b.size()));
        int i = 0, j = 0;
        while (out.size() < k && (i < a.size() || j < b.size())) {
            boolean takeA;
            if (i >= a.size()) takeA = false;
            else if (j >= b.size()) takeA = true;
            else takeA = Row.RANK_ORDER.compare(a.get(i), b.get(j)) <= 0;
            out.add(takeA ? a.get(i++) : b.get(j++));
        }
        return out;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof PartialState)) return false;
        return groups.equals(((PartialState) o).groups);
    }

    @Override
    public int hashCode() {
        return groups.hashCode();
    }
}
