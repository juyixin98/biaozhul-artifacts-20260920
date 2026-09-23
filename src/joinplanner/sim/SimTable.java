package joinplanner.sim;

/** A table to generate: name, row count, and per-endpoint columns (indexed by edge order). */
public final class SimTable {
    public final String name;
    public final long rows;
    public final ColumnSpec[] columns;

    public SimTable(String name, long rows, ColumnSpec[] columns) {
        this.name = name;
        this.rows = rows;
        this.columns = columns;
    }
}
