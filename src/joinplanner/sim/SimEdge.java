package joinplanner.sim;

/** An equi-join edge in a simulation scenario; indices reference the per-table column arrays. */
public final class SimEdge {
    public final String left;
    public final String right;
    public final int leftColumn;
    public final int rightColumn;
    public final Double explicitSelectivity; // optional override fed to the estimator
    public final String on;

    public int leftIndex = -1;
    public int rightIndex = -1;

    public SimEdge(String left, String right, int leftColumn, int rightColumn,
                   Double explicitSelectivity, String on) {
        this.left = left;
        this.right = right;
        this.leftColumn = leftColumn;
        this.rightColumn = rightColumn;
        this.explicitSelectivity = explicitSelectivity;
        this.on = on;
    }
}
