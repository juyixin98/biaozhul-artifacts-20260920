package joinplanner.model;

/**
 * A node in the binary join plan tree. Immutable.
 *
 * <p>Leaf nodes ({@code tableIndex >= 0}) scan a base table.
 * Join nodes combine two sub-plans: {@code edgeIndex} identifies the predicate for
 * {@link JoinType#INNER}; {@link JoinType#CROSS} has no predicate (selectivity 1).
 */
public final class PlanNode {

    private final int tableIndex;          // >= 0 for leaf
    private final PlanNode left;           // join children
    private final PlanNode right;
    private final JoinType joinType;       // null for leaf
    private final int edgeIndex;           // -1 for leaf / cross
    private final double outputRows;       // estimated cardinality of this node's output
    private final double nodeCost;         // cost accumulated at and below this node

    private PlanNode(int tableIndex, PlanNode left, PlanNode right,
                     JoinType joinType, int edgeIndex,
                     double outputRows, double nodeCost) {
        this.tableIndex = tableIndex;
        this.left = left;
        this.right = right;
        this.joinType = joinType;
        this.edgeIndex = edgeIndex;
        this.outputRows = outputRows;
        this.nodeCost = nodeCost;
    }

    /** Base-table scan. */
    public static PlanNode leaf(int tableIndex, double rows) {
        return new PlanNode(tableIndex, null, null, null, -1, rows, 0.0);
    }

    /** Binary join. */
    public static PlanNode join(PlanNode left, PlanNode right, JoinType type,
                                int edgeIndex, double outputRows, double nodeCost) {
        return new PlanNode(-1, left, right, type, edgeIndex, outputRows, nodeCost);
    }

    public boolean isLeaf() {
        return tableIndex >= 0;
    }

    public int tableIndex() {
        return tableIndex;
    }

    public PlanNode left() {
        return left;
    }

    public PlanNode right() {
        return right;
    }

    public JoinType joinType() {
        return joinType;
    }

    public int edgeIndex() {
        return edgeIndex;
    }

    public double outputRows() {
        return outputRows;
    }

    /** Summed size of every intermediate result at and below this node. */
    public double nodeCost() {
        return nodeCost;
    }
}
