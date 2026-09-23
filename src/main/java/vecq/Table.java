package vecq;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 内存表：若干等长列 + 行计数。
 *
 * JSON 形态：
 * <pre>
 * { "name": "t", "rowCount": 6,
 *   "columns": [ {"name":"id","type":"int","values":[1,null,3], "nullRows":[1]}, ... ] }
 * </pre>
 * NULL 有两种等价表达（values 中写 null，或用 nullRows 位图），但同一列必须一致。
 */
public final class Table {

    private final String name;
    private final List<Column> columns;
    private final int rowCount;

    public Table(String name, List<Column> columns) {
        this.name = name;
        this.columns = List.copyOf(columns);
        int rc = -1;
        for (Column c : this.columns) {
            if (rc == -1) rc = c.size();
            else if (c.size() != rc) {
                throw new InvalidQueryException("表 " + name + " 的列 " + c.name()
                        + " 长度 " + c.size() + " 与其它列长度 " + rc + " 不一致");
            }
        }
        this.rowCount = rc < 0 ? 0 : rc;
    }

    public String name() { return name; }
    public int rowCount() { return rowCount; }
    public List<Column> columns() { return columns; }

    public Column column(String colName) {
        for (Column c : columns) {
            if (c.name().equals(colName)) return c;
        }
        throw new InvalidQueryException("表 " + name + " 中不存在列 \"" + colName
                + "\"；可用列: " + columnNames());
    }

    public List<String> columnNames() {
        List<String> names = new ArrayList<>();
        for (Column c : columns) names.add(c.name());
        return names;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("rowCount", rowCount);
        List<Object> cols = new ArrayList<>();
        for (Column c : columns) cols.add(c.toJson());
        m.put("columns", cols);
        return m;
    }

    @SuppressWarnings("unchecked")
    public static Table fromJson(Object o) {
        if (o instanceof String s) {
            // 允许引用已注册的表名，由 QueryEngine 处理；这里先抛错
            throw new InvalidQueryException("表引用 \"" + s + "\" 需要在表目录中存在");
        }
        Map<String, Object> obj = Json.asMap(o);
        String name = obj.containsKey("name") ? String.valueOf(obj.get("name")) : "inline";
        Object colsObj = obj.get("columns");
        if (colsObj == null) throw new InvalidQueryException("内联表缺少 columns 字段");
        List<Object> colList = Json.asList(colsObj);
        List<Column> cols = new ArrayList<>();
        for (Object c : colList) {
            cols.add(Column.fromJson(Json.asMap(c)));
        }
        if (cols.isEmpty()) throw new InvalidQueryException("表 " + name + " 至少需要一列");
        Table t = new Table(name, cols);
        Object declared = obj.get("rowCount");
        if (declared instanceof Number n && n.intValue() != t.rowCount()) {
            throw new InvalidQueryException("表 " + name + " 声明 rowCount=" + n.intValue()
                    + " 与列实际长度 " + t.rowCount() + " 不一致");
        }
        return t;
    }
}
