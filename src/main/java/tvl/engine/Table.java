package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.DataType;
import tvl.core.Value;

/**
 * 单机内存表：有序列定义 + 若干不可变结构的行。
 *
 * 行用 {@code Value[]} 表示，下标与 columns 一致。
 * 表对象可被导出为 JSON（数据可导出要求）。
 */
public final class Table {

    public static final class Column {
        public final String name;
        public final DataType type;

        public Column(String name, DataType type) {
            this.name = name;
            this.type = type;
        }

        public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", name);
            m.put("type", type.name());
            return m;
        }
    }

    private final String name;
    private final List<Column> columns;
    private final List<Value[]> rows;
    private final Map<String, Integer> index = new LinkedHashMap<>();

    public Table(String name, List<Column> columns) {
        this.name = name;
        this.columns = new ArrayList<>(columns);
        this.rows = new ArrayList<>();
        for (int i = 0; i < this.columns.size(); i++) {
            String n = this.columns.get(i).name;
            if (index.put(n, i) != null) {
                throw new IllegalArgumentException("列名重复：" + n);
            }
        }
    }

    public String name() {
        return name;
    }

    public List<Column> columns() {
        return columns;
    }

    public List<Value[]> rows() {
        return rows;
    }

    public int columnIndex(String name) {
        Integer idx = index.get(name);
        return idx == null ? -1 : idx;
    }

    public void addRow(Value[] row) {
        if (row.length != columns.size()) {
            throw new IllegalArgumentException("行列数不匹配：期望 "
                    + columns.size() + "，实际 " + row.length);
        }
        rows.add(row);
    }

    /** 列名 -> 类型（顺序与列定义一致）。 */
    public Map<String, DataType> schemaMap() {
        Map<String, DataType> m = new LinkedHashMap<>();
        for (Column c : columns) {
            m.put(c.name, c.type);
        }
        return m;
    }

    public String[] columnNames() {
        String[] arr = new String[columns.size()];
        for (int i = 0; i < arr.length; i++) {
            arr[i] = columns.get(i).name;
        }
        return arr;
    }

    /** 导出为 JSON 结构（含列定义与全部行）。 */
    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        List<Object> cols = new ArrayList<>();
        for (Column c : columns) {
            cols.add(c.toJson());
        }
        m.put("columns", cols);

        List<Object> rowList = new ArrayList<>();
        for (Value[] row : rows) {
            List<Object> r = new ArrayList<>();
            for (Value v : row) {
                r.add(v.toJson());
            }
            rowList.add(r);
        }
        m.put("rowCount", rows.size());
        m.put("rows", rowList);
        return m;
    }
}
