package joinplanner.sim;

import joinplanner.model.CostModel;
import joinplanner.model.Edge;
import joinplanner.model.Table;

import java.util.List;

/**
 * Dynamic program identical in structure to {@link joinplanner.plan.Planner} but fed by
 * measured cardinalities. Keeping it independent (instead of reusing PlanNode) makes
 * "estimated optimum" and "true optimum" genuinely separate computations.
 */
public final class ActualPlanner {

    public static final class ActualPlanNode {
        public final boolean leaf;
        public final int tableIndex;
        public final ActualPlanNode left;
        public final ActualPlanNode right;
        public final int mask;
        public final long rows;
        public final double nodeCost;

        private ActualPlanNode(boolean leaf, int tableIndex, ActualPlanNode left, ActualPlanNode right,
                               int mask, long rows, double nodeCost) {
            this.leaf = leaf;
            this.tableIndex = tableIndex;
            this.left = left;
            this.right = right;
            this.mask = mask;
            this.rows = rows;
            this.nodeCost = nodeCost;
        }

        static ActualPlanNode leaf(int idx, int mask, long rows) {
            return new ActualPlanNode(true, idx, null, null, mask, rows, 0);
        }

        static ActualPlanNode join(ActualPlanNode l, ActualPlanNode r, int mask, long rows, double cost) {
            return new ActualPlanNode(false, -1, l, r, mask, rows, cost);
        }
    }

    /** Public result carrier matching the estimated-side {@code DpSolution} shape. */
    public static final class DpResult {
        public final ActualPlanNode tree;
        public final double cost;

        public DpResult(ActualPlanNode tree, double cost) {
            this.tree = tree;
            this.cost = cost;
        }
    }

    private static final class Sol {
        final ActualPlanNode tree;
        final double cost;

        Sol(ActualPlanNode tree, double cost) {
            this.tree = tree;
            this.cost = cost;
        }
    }

    private final ActualCostModel model;
    private final Sol[] best;

    public ActualPlanner(ActualCostModel model, List<Table> tables, List<Edge> edges) {
        this.model = model;
        this.best = new Sol[1 << tables.size()];
    }

    public DpResult solve(int targetMask) {
        Sol s = solveRec(targetMask);
        return new DpResult(s.tree, s.cost);
    }

    private Sol solveRec(int mask) {
        if (best[mask] != null) {
            return best[mask];
        }
        if (Integer.bitCount(mask) == 1) {
            int i = Integer.numberOfTrailingZeros(mask);
            Sol s = new Sol(ActualPlanNode.leaf(i, mask, (long) model.card(mask)), 0);
            best[mask] = s;
            return s;
        }
        double bestCost = Double.POSITIVE_INFINITY;
        Sol choice = null;
        int anchor = Integer.lowestOneBit(mask);
        int rest = mask ^ anchor;
        for (int sub = rest;; sub = (sub - 1) & rest) {
            int l = anchor | sub;
            int r = mask ^ l;
            if (r != 0 && model.connected(l) && model.connected(r) && model.hasCrossingEdge(l, r)) {
                Sol ls = solveRec(l);
                Sol rs = solveRec(r);
                double nodeCost = switch (model.costModel) {
                    case TOTAL_INTERMEDIATE_ROWS -> model.card(mask);
                    case SUM_OF_INPUTS -> model.card(l) + model.card(r);
                };
                double total = ls.cost + rs.cost + nodeCost;
                if (total < bestCost) {
                    bestCost = total;
                    ActualPlanNode node = ActualPlanNode.join(ls.tree, rs.tree, mask,
                            (long) model.card(mask), nodeCost);
                    choice = new Sol(node, total);
                }
            }
            if (sub == 0) {
                break;
            }
        }
        if (choice == null) {
            throw new IllegalStateException("No actual decomposition for mask " + mask);
        }
        best[mask] = choice;
        return choice;
    }
}
