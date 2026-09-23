package joinplanner.plan;

import joinplanner.model.Spec;

import java.util.ArrayList;
import java.util.List;

/**
 * Exhaustive enumeration used to independently verify the dynamic program.
 *
 * <p><b>Left-deep plans</b> are enumerated as permutations of table indices: a
 * permutation is legal when every prefix (after the first) is connected. The cost of
 * the permutation is the cost of the corresponding left-deep join tree.
 *
 * <p><b>All binary plans (including bushy)</b> are enumerated recursively as
 * bipartitions; the cost of a join node equals the DP's node cost, so an exhaustive
 * minimum must match {@link Planner} exactly.
 */
public final class BruteForce {

    private final Spec spec;
    private final Estimator est;

    public BruteForce(Spec spec, Estimator est) {
        this.spec = spec;
        this.est = est;
    }

    /** Every legal left-deep order with its cost. Only meaningful for connected graphs. */
    public List<OrderCost> enumerateLeftDeepOrders() {
        int n = spec.n();
        List<OrderCost> results = new ArrayList<>();
        int[] perm = new int[n];
        boolean[] used = new boolean[n];
        for (int start = 0; start < n; start++) {
            perm[0] = start;
            used[start] = true;
            recurse(1, perm, used, results);
            used[start] = false;
        }
        return results;
    }

    private void recurse(int depth, int[] perm, boolean[] used, List<OrderCost> out) {
        int n = spec.n();
        if (depth == n) {
            out.add(scoreLeftDeep(perm));
            return;
        }
        for (int t = 0; t < n; t++) {
            if (used[t]) {
                continue;
            }
            int mask = 0;
            for (int i = 0; i < depth; i++) {
                mask |= 1 << perm[i];
            }
            // Legal iff the new table has a join edge into the set already present.
            if (!est.hasCrossingEdge(1 << t, mask)) {
                continue;
            }
            perm[depth] = t;
            used[t] = true;
            recurse(depth + 1, perm, used, out);
            used[t] = false;
        }
    }

    private OrderCost scoreLeftDeep(int[] perm) {
        double total = 0;
        int mask = 1 << perm[0];
        boolean saturated = est.saturated(mask);
        List<String> names = new ArrayList<>();
        names.add(spec.tables.get(perm[0]).name());
        for (int k = 1; k < perm.length; k++) {
            int newMask = mask | (1 << perm[k]);
            total += nodeCost(mask, 1 << perm[k], newMask);
            mask = newMask;
            saturated |= est.saturated(mask);
            names.add(spec.tables.get(perm[k]).name());
        }
        return new OrderCost(names, total, saturated);
    }

    private double nodeCost(int l, int r, int result) {
        return switch (spec.costModel) {
            case TOTAL_INTERMEDIATE_ROWS -> est.card(result);
            case SUM_OF_INPUTS -> est.card(l) + est.card(r);
        };
    }

    /** Minimum cost over every binary tree (bushy allowed). */
    public double bestBushyCost(int mask) {
        if (Integer.bitCount(mask) == 1) {
            return 0;
        }
        double best = Double.POSITIVE_INFINITY;
        int anchor = Integer.lowestOneBit(mask);
        int rest = mask ^ anchor;
        for (int sub = rest;; sub = (sub - 1) & rest) {
            int l = anchor | sub;
            int r = mask ^ l;
            if (r != 0 && est.connected(l) && est.connected(r) && est.hasCrossingEdge(l, r)) {
                double total = bestBushyCost(l) + bestBushyCost(r) + nodeCost(l, r, mask);
                best = Math.min(best, total);
            }
            if (sub == 0) {
                break;
            }
        }
        return best;
    }

    /** A legal left-deep order and the total cost its join tree incurs. */
    public record OrderCost(List<String> order, double cost, boolean saturated) {
    }
}
