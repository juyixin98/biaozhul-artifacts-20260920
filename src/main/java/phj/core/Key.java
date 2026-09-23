package phj.core;

import java.util.ArrayList;
import java.util.List;

/**
 * 多列连接键。任意一列是 NULL 即视为 NULL 键（参与分区落盘但永不匹配）。
 * 不可变；equals/hashCode 委托给各列 Value，遵循 Value 的数值跨型语义。
 */
public final class Key {

    public static final Key NULL_KEY = new Key(List.of());

    private final List<Value> columns;
    private final boolean isNull;

    public Key(List<Value> columns) {
        this.columns = List.copyOf(columns);
        boolean n = this.columns.isEmpty();
        for (Value c : this.columns) {
            if (c.isNull()) { n = true; break; }
        }
        this.isNull = n;
    }

    public List<Value> columns() { return columns; }

    public int size() { return columns.size(); }

    public boolean isNull() { return isNull; }

    public static Key ofRow(Row row, int[] indices) {
        List<Value> cols = new ArrayList<>(indices.length);
        for (int idx : indices) cols.add(row.get(idx));
        return new Key(cols);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Key k)) return false;
        // SQL 语义：键含 NULL 永不匹配（NULL = NULL 为 UNKNOWN）。
        if (isNull || k.isNull) return false;
        return columns.equals(k.columns); // Value.equals 已处理数值跨型
    }

    @Override
    public int hashCode() {
        int h = 1;
        for (Value c : columns) h = 31 * h + c.hashCode();
        return h;
    }

    @Override
    public String toString() {
        return columns.toString();
    }
}
