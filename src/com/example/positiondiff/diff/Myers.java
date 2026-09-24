package com.example.positiondiff.diff;

import com.example.positiondiff.model.Line;

import java.util.ArrayList;
import java.util.List;

/**
 * Line-level Myers diff (Myers 1986, "An O(ND) Difference Algorithm"),
 * forward search with a V-array and full trace backtracking.
 *
 * <p>The search can be bounded by a {@link Budget}. When the budget runs out
 * before the endpoint (N, M) is reached, {@link MyersResult#exhausted()} is
 * set and no op list is produced — the caller is expected to fall back.
 */
public final class Myers {

    /** Raw op carrying only type and line; positions are assigned by the engine. */
    public record RawOp(EditOp.Type type, Line line) {}

    public static final class MyersResult {
        private final List<RawOp> ops;   // forward order; null when exhausted
        private final boolean exhausted;
        private final long nodesUsed;
        private final int distance;

        private MyersResult(List<RawOp> ops, boolean exhausted, long nodesUsed, int distance) {
            this.ops = ops;
            this.exhausted = exhausted;
            this.nodesUsed = nodesUsed;
            this.distance = distance;
        }

        public List<RawOp> ops() { return ops; }
        public boolean exhausted() { return exhausted; }
        public long nodesUsed() { return nodesUsed; }
        public int distance() { return distance; }
    }

    private Myers() {}

    public static MyersResult diff(List<Line> a, List<Line> b, Budget budget) {
        int n = a.size();
        int m = b.size();
        int max = n + m;
        if (max == 0) {
            return new MyersResult(new ArrayList<>(), false, 0, 0);
        }

        int offset = max;
        int[] v = new int[2 * max + 1];   // index for k is (k + offset)
        v[1 + offset] = 0;

        List<int[]> trace = new ArrayList<>();
        long nodesUsed = 0;

        for (int d = 0; d <= max; d++) {
            if (!budget.dUnlimited() && d > budget.maxD()) {
                return new MyersResult(null, true, nodesUsed, d - 1);
            }
            trace.add(v.clone());
            for (int k = -d; k <= d; k += 2) {
                int x;
                if (k == -d || (k != d && v[k - 1 + offset] < v[k + 1 + offset])) {
                    x = v[k + 1 + offset];
                } else {
                    x = v[k - 1 + offset] + 1;
                }
                int y = x - k;
                while (x < n && y < m && a.get(x).equals(b.get(y))) {
                    x++;
                    y++;
                }
                v[k + offset] = x;
                if (x >= n && y >= m) {
                    List<RawOp> ops = backtrack(trace, a, b, v, offset);
                    return new MyersResult(ops, false, nodesUsed, d);
                }
                // Charge this endpoint only after it failed to finish, so a
                // budget of 0 still admits the trivial d=0 equal-files case.
                if (!budget.nodesUnlimited()) {
                    nodesUsed++;
                    if (nodesUsed > budget.maxNodes()) {
                        return new MyersResult(null, true, nodesUsed, d);
                    }
                }
            }
        }
        // Unreachable: endpoint is always found by d = N + M.
        throw new IllegalStateException("myers search failed to reach endpoint");
    }

    private static List<RawOp> backtrack(List<int[]> trace, List<Line> a, List<Line> b,
                                         int[] finalV, int offset) {
        int n = a.size();
        int m = b.size();
        int x = n;
        int y = m;
        List<RawOp> reversed = new ArrayList<>();

        for (int d = trace.size() - 1; d > 0; d--) {
            int[] v = trace.get(d);
            int k = x - y;
            int prevK;
            if (k == -d || (k != d && v[k - 1 + offset] < v[k + 1 + offset])) {
                prevK = k + 1;
            } else {
                prevK = k - 1;
            }
            int prevX = v[prevK + offset];
            int prevY = prevX - prevK;

            while (x > prevX && y > prevY) {
                reversed.add(new RawOp(EditOp.Type.EQUAL, a.get(x - 1)));
                x--;
                y--;
            }
            if (x == prevX) {
                reversed.add(new RawOp(EditOp.Type.INSERT, b.get(prevY)));
            } else {
                reversed.add(new RawOp(EditOp.Type.DELETE, a.get(prevX)));
            }
            x = prevX;
            y = prevY;
        }
        while (x > 0 && y > 0) {
            reversed.add(new RawOp(EditOp.Type.EQUAL, a.get(x - 1)));
            x--;
            y--;
        }

        List<RawOp> ops = new ArrayList<>(reversed.size());
        for (int i = reversed.size() - 1; i >= 0; i--) {
            ops.add(reversed.get(i));
        }
        return ops;
    }
}
