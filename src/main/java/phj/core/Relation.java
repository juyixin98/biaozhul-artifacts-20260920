package phj.core;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** 关系：列名 + 行集合。列按位置索引；允许重名列（按位置仍可区分）。 */
public final class Relation {

    private final String name;
    private final List<String> columns;
    private final List<Row> rows;

    public Relation(String name, List<String> columns, List<Row> rows) {
        this.name = name;
        this.columns = List.copyOf(columns);
        this.rows = new ArrayList<>(rows);
    }

    public String name() { return name; }

    public List<String> columns() { return columns; }

    public int width() { return columns.size(); }

    public List<Row> rows() { return Collections.unmodifiableList(rows); }

    public int rowCount() { return rows.size(); }

    public int columnIndex(String col) {
        int found = -1;
        for (int i = 0; i < columns.size(); i++) {
            if (columns.get(i).equals(col)) {
                if (found != -1) {
                    throw new IllegalArgumentException(
                            "关系 '" + name + "' 中列名 '" + col + "' 不唯一，无法按名解析连接键");
                }
                found = i;
            }
        }
        return found;
    }

    /**
     * 从请求 JSON 构造关系。
     * 形如 {"name":"orders","columns":["id","k"],"rows":[[1,"a"],[2,null]]}
     */
    @SuppressWarnings("unchecked")
    public static Relation fromJson(Object obj) {
        var m = phj.json.Json.asObj(obj, "关系定义");
        String name = phj.json.Json.optStr(m, "name", "?");
        Object colsObj = m.get("columns");
        if (colsObj == null) throw new IllegalArgumentException("关系 '" + name + "' 缺少 columns");
        List<Object> colArr = phj.json.Json.asArr(colsObj, "关系 '" + name + "' 的 columns");
        List<String> cols = new ArrayList<>();
        for (Object c : colArr) cols.add(phj.json.Json.asStr(c, "关系 '" + name + "' 的列名"));
        Object rowsObj = m.get("rows");
        List<Object> rowArr = rowsObj == null ? List.of() : phj.json.Json.asArr(rowsObj, "关系 '" + name + "' 的 rows");
        List<Row> rows = new ArrayList<>(rowArr.size());
        int r = 0;
        for (Object rowObj : rowArr) {
            List<Object> cells = phj.json.Json.asArr(rowObj, "关系 '" + name + "' 第 " + r + " 行");
            if (cells.size() != cols.size()) {
                throw new IllegalArgumentException(
                        "关系 '" + name + "' 第 " + r + " 行列数 " + cells.size()
                                + " 与表头列数 " + cols.size() + " 不一致");
            }
            List<Value> vals = new ArrayList<>(cells.size());
            for (Object cell : cells) vals.add(Value.fromJson(cell));
            rows.add(new Row(vals));
            r++;
        }
        return new Relation(name, cols, rows);
    }
}
