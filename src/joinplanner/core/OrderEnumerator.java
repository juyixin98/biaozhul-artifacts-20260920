package joinplanner.core;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Brute-force enumeration used to verify the dynamic program and to build the
 * "every legal order" explanation.
 *
 * <ul>
 *   <li><b>left-deep orders</b>: every permutation of tables that adds each new
 *       table along at least one join edge to the already-built prefix. The cost
 *       of a left-deep order is Σ cardinality(prefix-of-length-k).</li>
 *   <li><b>all binary (bushy) plans</b>: recursively split every connected
 *       subset along crossing edges; used only for small n in tests to prove the
 *       DP's optimum.</li>
 * </ul>
 */
public final class OrderEnumerator {

    private final ValidatedProblem p;
    private final int n;
    private final int all;
    private final double[] subsetLogCard;

    public OrderEnumerator(ValidatedProblem p, JoinPlanner planner) {
        this.p = p;
        this.n = p.n();
        this.all = (1 << n) - 1;
        double[] cards = new double[1 << n];
        for (int m = 0; m < (1 << n); m++) {
            cards[m] = planner.subsetLogCardinality(m);
        }
        this.subsetLogCard = cards;
    }

    /** One evaluated left-deep order. */
    public record LeftDeepOrder(List<String> order, double cost, boolean legal)
            implements Comparable<LeftDeepOrder> {
        @Override
        public int compareTo(LeftDeepOrder o) {
            int c = Double.compare(cost, o.cost);
            if (c != 0) {
                return c;
            }
            return order.toString().compareTo(o.order.toString());
        }
    }

    /** Evaluate all n! permutations; illegal ones are flagged rather than dropped. */
    public List<LeftDeepOrder> enumerateLeftDeep() {
        List<LeftDeepOrder> out = new ArrayList<>();
        int[] perm = new int[n];
        for (int i = 0; i < n; i++) {
            perm[i] = i;
        }
        permute(perm, 0, out);
        out.sort(null);
        return out;
    }

    private void permute(int[] perm, int k, List<LeftDeepOrder> out) {
        if (k == n) {
            evaluate(perm, out);
            return;
        }
        for (int i = k; i < n; i++) {
            swap(perm, k, i);
            permute(perm, k + 1, out);
            swap(perm, k, i);
        }
    }

    private void swap(int[] a, int i, int j) {
        int t = a[i];
        a[i] = a[j];
        a[j] = t;
    }

    private void evaluate(int[] perm, List<LeftDeepOrder> out) {
        int mask = 1 << perm[0];
        double logCost = Double.NEGATIVE_INFINITY; // running sum in log domain
        boolean legal = true;
        for (int k = 1; k < n; k++) {
            int t = perm[k];
            if (!hasEdgeTo(t, mask)) {
                legal = false;
                break;
            }
            mask |= 1 << t;
            logCost = Stats.logAdd(logCost, subsetLogCard[mask]);
        }
        List<String> names = Arrays.stream(perm)
                .mapToObj(i -> p.spec().tables().get(i).name())
                .toList();
        out.add(new LeftDeepOrder(names, Stats.fromLog(logCost), legal));
    }

    private boolean hasEdgeTo(int table, int mask) {
        for (int[] nb : p.adjacency().get(table)) {
            if ((mask & (1 << nb[0])) != 0) {
                return true;
            }
        }
        return false;
    }

    /**
     * Minimum cost over ALL binary join trees (bushy space), by exhaustive
     * recursion. Returns {@code {logCost, count}} — count is the number of
     * distinct optimal trees. For tests cross-checking the DP.
     */
    public double[] bestBushyBruteForce() {
        return bestBushy(all);
    }

    private double[] bestBushy(int mask) {
        if (Integer.bitCount(mask) == 1) {
            return new double[]{Double.NEGATIVE_INFINITY, 1};
        }
        double best = Double.POSITIVE_INFINITY;
        long count = 0;
        int sub = (mask - 1) & mask;
        while (sub != 0) {
            int other = mask ^ sub;
            int lowBit = mask & -mask;
            if ((sub & lowBit) != 0 && other != 0 && hasCrossingEdge(sub, other)) {
                double[] a = bestBushy(sub);
                double[] b = bestBushy(other);
                double logCost = Stats.logAdd(Stats.logAdd(a[0], b[0]),
                        subsetLogCard[mask]);
                if (logCost < best) {
                    best = logCost;
                    count = (long) a[1] * (long) b[1];
                } else if (logCost == best) {
                    count += (long) a[1] * (long) b[1];
                }
            }
            sub = (sub - 1) & mask;
        }
        return new double[]{best, count};
    }

    private boolean hasCrossingEdge(int a, int b) {
        for (var e : p.resolvedEdges()) {
            int ea = 1 << e.leftIndex();
            int eb = 1 << e.rightIndex();
            boolean aHasL = (a & ea) != 0;
            boolean aHasR = (a & eb) != 0;
            boolean bHasL = (b & ea) != 0;
            boolean bHasR = (b & eb) != 0;
            if ((aHasL && bHasR) || (aHasR && bHasL)) {
                return true;
            }
        }
        return false;
    }
}
