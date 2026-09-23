package joinplanner.plan;

import joinplanner.model.CostModel;
import joinplanner.model.Edge;
import joinplanner.model.Spec;

import java.util.ArrayList;
import java.util.List;

/**
 * Dense-subset dynamic program for join ordering.
 *
 * <p>For every connected mask M, {@code best[M]} is the cheapest plan producing M,
 * built from the best plans of a bipartition M = L ∪ R where both sides are connected
 * and at least one join edge crosses the cut. Ties are broken deterministically by the
 * smaller left mask so results are reproducible.
 *
 * <p>With {@code leftDeepOnly} the right side must be a single table.
 *
 * <p>Costs use saturated arithmetic through {@link Estimator#card(int)}; any plan whose
 * mask saturated is flagged so callers can report the overflow.
 */
public final class Planner {

    private final Spec spec;
    private final Estimator est;
    private final DpSolution[] best;
    private final int n;

    public Planner(Spec spec, Estimator est) {
        this.spec = spec;
        this.est = est;
        this.n = spec.n();
        this.best = new DpSolution[1 << n];
    }

    public DpSolution solve(int targetMask) {
        if (best[targetMask] != null) {
            return best[targetMask];
        }
        if (Integer.bitCount(targetMask) == 1) {
            int i = Integer.numberOfTrailingZeros(targetMask);
            PlanNode leaf = PlanNode.leaf(spec.tables.get(i).name(), i, targetMask, est.card(targetMask));
            DpSolution s = new DpSolution(leaf, 0.0, est.saturated(targetMask));
            best[targetMask] = s;
            return s;
        }

        double bestCost = Double.POSITIVE_INFINITY;
        DpSolution bestChoice = null;
        // Enumerate bipartitions: fix the anchor on the left, then take every non-empty
        // proper sub-mask of the target as l (l contains anchor; r = target\l).
        int anchor = Integer.lowestOneBit(targetMask);
        for (int l = (targetMask - 1) & targetMask; l != 0; l = (l - 1) & targetMask) {
            if ((l & anchor) == 0) {
                continue;
            }
            int r = targetMask ^ l;
            if (r == 0) {
                continue;
            }
            // Left-deep: right side must be a single table. (Right-deep plans have the
            // same cost by commutativity, so restricting here loses no optimum.)
            if (spec.leftDeepOnly && Integer.bitCount(r) != 1) {
                continue;
            }
            if (!est.connected(l) || !est.connected(r) || !est.hasCrossingEdge(l, r)) {
                continue;
            }
            DpSolution ls = solve(l);
            DpSolution rs = solve(r);

            double nodeCost = switch (spec.costModel) {
                case TOTAL_INTERMEDIATE_ROWS -> est.card(targetMask);
                case SUM_OF_INPUTS -> est.card(l) + est.card(r);
            };
            double total = ls.cost() + rs.cost() + nodeCost;
            if (total < bestCost) {
                bestCost = total;
                List<String> edgeLabels = crossingEdgeLabels(l, r);
                PlanNode node = PlanNode.join(ls.tree(), rs.tree(), targetMask,
                        est.card(targetMask), nodeCost, edgeLabels);
                bestChoice = new DpSolution(node, total,
                        ls.saturated() || rs.saturated() || est.saturated(targetMask));
            }
        }

        if (bestChoice == null) {
            // Connected mask with no valid split is impossible for |M|>=2 (spanning-tree edge
            // always yields a crossing split); guard anyway.
            throw new IllegalStateException("No join decomposition found for mask "
                    + Integer.toBinaryString(targetMask));
        }
        best[targetMask] = bestChoice;
        return bestChoice;
    }

    private List<String> crossingEdgeLabels(int l, int r) {
        List<String> labels = new ArrayList<>();
        for (Edge e : spec.edges) {
            boolean ll = (l & (1 << e.leftIndex)) != 0;
            boolean lr = (l & (1 << e.rightIndex)) != 0;
            boolean rl = (r & (1 << e.leftIndex)) != 0;
            boolean rr = (r & (1 << e.rightIndex)) != 0;
            if ((ll && rr) || (lr && rl)) {
                labels.add(e.on != null
                        ? e.on
                        : e.left + " = " + e.right);
            }
        }
        return labels;
    }
}
