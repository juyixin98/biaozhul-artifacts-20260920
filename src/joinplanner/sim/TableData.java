package joinplanner.sim;

/** A generated base table: column values per referenced column, indexed by row. */
public final class TableData {

    private final String name;
    // column name -> values[row]
    private final java.util.Map<String, int[]> columns = new java.util.LinkedHashMap<>();

    public TableData(String name) {
        this.name = name;
    }

    public String name() {
        return name;
    }

    public void putColumn(String column, int[] values) {
        columns.put(column, values);
    }

    public int[] column(String column) {
        return columns.get(column);
    }

    public java.util.Set<String> columns() {
        return columns.keySet();
    }

    public int rows() {
        int any = columns.values().iterator().next().length;
        return any;
    }
}
