package joinplanner.plan;

import java.util.List;

/**
 * One node of a binary join plan tree. A leaf scans one base table; an internal node
 * joins two sub-plans over the edges listed in {@code joinEdges}.
 */
public record PlanNode(
        boolean leaf,
        String table,          // leaf only
        int tableIndex,        // leaf only; -1 for joins
        PlanNode left,         // join only
        PlanNode right,        // join only
        int mask,
        double outputRows,
        double nodeCost,
        List<String> joinEdges // human-readable predicates applied at this join
) {
    public static PlanNode leaf(String table, int tableIndex, int mask, double rows) {
        return new PlanNode(true, table, tableIndex, null, null, mask, rows, 0.0, List.of());
    }

    public static PlanNode join(PlanNode left, PlanNode right, int mask, double outputRows,
                                double nodeCost, List<String> joinEdges) {
        return new PlanNode(false, null, -1, left, right, mask, outputRows, nodeCost, joinEdges);
    }
}
