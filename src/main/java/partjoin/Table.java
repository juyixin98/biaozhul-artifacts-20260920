package partjoin;

import java.util.ArrayList;
import java.util.List;

/** An in-memory relation: a schema plus a list of rows. */
public final class Table {

    private final String name;
    private final Schema schema;
    private final List<Row> rows;

    public Table(String name, Schema schema, List<Row> rows) {
        this.name = name == null ? "" : name;
        this.schema = schema;
        List<Row> copy = new ArrayList<>(rows);
        for (Row r : copy) {
            if (r.size() != schema.size()) {
                throw new JoinException(JoinException.INVALID_REQUEST,
                        "Row width " + r.size() + " does not match " + schema.size()
                                + " columns in table '" + this.name + "'");
            }
        }
        this.rows = List.copyOf(copy);
    }

    public String name() {
        return name;
    }

    public Schema schema() {
        return schema;
    }

    public List<Row> rows() {
        return rows;
    }

    public int rowCount() {
        return rows.size();
    }
}
