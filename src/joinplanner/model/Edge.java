package joinplanner.model;

/**
 * An equi-join predicate between two tables.
 *
 * <p>Selectivity may be supplied explicitly ({@code selectivity}); otherwise it is derived
 * from NDV / uniqueness information (see {@code Estimator}). {@code leftUnique} /
 * {@code rightUnique} mean the column on that end is a unique key, which bounds the
 * selectivity of the join by the other side's cardinality.
 */
public final class Edge {
    public final String left;
    public final String right;
    public final Double selectivity;   // may be null -> derive
    public final Long leftNdv;         // distinct values on left column
    public final Long rightNdv;
    public final boolean leftUnique;
    public final boolean rightUnique;
    public final String on;            // human-readable predicate label

    public int leftIndex = -1;
    public int rightIndex = -1;
    public double derivedSelectivity = Double.NaN;
    public String selectivitySource;   // explanation of where the number came from

    public Edge(String left, String right, Double selectivity, Long leftNdv, Long rightNdv,
                boolean leftUnique, boolean rightUnique, String on) {
        this.left = left;
        this.right = right;
        this.selectivity = selectivity;
        this.leftNdv = leftNdv;
        this.rightNdv = rightNdv;
        this.leftUnique = leftUnique;
        this.rightUnique = rightUnique;
        this.on = on;
    }

    /** Index of the endpoint that is not {@code idx}; used for adjacency expansion. */
    public int other(int idx) {
        return idx == leftIndex ? rightIndex : leftIndex;
    }
}
