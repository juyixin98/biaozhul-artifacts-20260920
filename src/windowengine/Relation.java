package windowengine;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 内存关系：Schema + 行集合。行顺序即输入顺序，引擎全程保留该顺序。
 */
public final class Relation {

    private final Schema schema;
    private final List<Row> rows;

    public Relation(Schema schema, List<Row> rows) {
        this.schema = schema;
        this.rows = new ArrayList<>(rows);
    }

    public Schema schema() {
        return schema;
    }

    public List<Row> rows() {
        return Collections.unmodifiableList(rows);
    }

    public int rowCount() {
        return rows.size();
    }
}
