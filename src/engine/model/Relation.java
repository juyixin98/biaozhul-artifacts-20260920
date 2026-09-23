package engine.model;

import java.util.List;

/**
 * 内存中的关系：带 schema 的行集合（schema 不可变）。
 *
 * <p>每个单元值只能是 {@code null}、{@code Long} 或 {@code String}，
 * 具体由对应列的 {@link ColumnType} 决定。每行单独保存 {@code sourceIndex}，
 * 即使经过分区、排序，也能追溯到输入中的原始位置；
 * ROW_NUMBER 在排序键完全相同时以原始位置作为最终决胜键（稳定排序）。
 */
public final class Relation {

    /** 一行数据。 */
    public static final class Row {
        public final long sourceIndex;
        public final Object[] values;

        public Row(long sourceIndex, Object[] values) {
            this.sourceIndex = sourceIndex;
            this.values = values;
        }

        public Object get(int col) {
            return values[col];
        }
    }

    private final List<String> columnNames;
    private final List<ColumnType> columnTypes;
    private final List<Row> rows;

    public Relation(List<String> columnNames, List<ColumnType> columnTypes, List<Row> rows) {
        if (columnNames.size() != columnTypes.size()) {
            throw new IllegalArgumentException("列名与列类型数量不一致");
        }
        this.columnNames = List.copyOf(columnNames);
        this.columnTypes = List.copyOf(columnTypes);
        this.rows = rows;
    }

    public List<String> columnNames() {
        return columnNames;
    }

    public List<ColumnType> columnTypes() {
        return columnTypes;
    }

    public List<Row> rows() {
        return rows;
    }

    public int columnCount() {
        return columnNames.size();
    }

    public int columnIndex(String name) {
        for (int i = 0; i < columnNames.size(); i++) {
            if (columnNames.get(i).equals(name)) {
                return i;
            }
        }
        throw new IllegalArgumentException("列不存在: " + name);
    }
}
