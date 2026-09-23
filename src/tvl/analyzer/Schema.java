package tvl.analyzer;

import java.util.LinkedHashMap;
import java.util.Map;

/** 列名 -> 列类型的有序映射。 */
public final class Schema {
    private final Map<String, DataType> columns;

    public Schema() {
        this.columns = new LinkedHashMap<>();
    }

    public static Schema of(String name1, DataType t1) {
        Schema s = new Schema();
        s.add(name1, t1);
        return s;
    }

    public static Schema of(String name1, DataType t1, String name2, DataType t2) {
        Schema s = new Schema();
        s.add(name1, t1);
        s.add(name2, t2);
        return s;
    }

    public void add(String name, DataType type) {
        columns.put(name, type);
    }

    public DataType typeOf(String column) {
        return columns.get(column);
    }

    public boolean has(String column) {
        return columns.containsKey(column);
    }

    public java.util.Set<String> columnNames() {
        return columns.keySet();
    }

    public int size() {
        return columns.size();
    }
}
