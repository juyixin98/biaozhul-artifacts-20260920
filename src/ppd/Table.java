package ppd;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** 基表：名字 + 列名（限定名用表名或别名）+ 行数据。 */
public class Table {

    private final String qualifier;
    private final List<String> columnNames;
    private final List<Row> rows;

    public Table(String qualifier, List<String> columnNames, List<Row> rows) {
        this.qualifier = qualifier;
        this.columnNames = List.copyOf(columnNames);
        this.rows = new ArrayList<>(rows);   // 行数据允许含 NULL，不能用 List.copyOf
        for (Row r : rows) {
            if (r.width() != columnNames.size()) {
                throw new EngineException("表 " + qualifier + " 行宽 " + r.width()
                        + " 与列数 " + columnNames.size() + " 不一致");
            }
        }
    }

    public String qualifier() { return qualifier; }

    public List<String> columnNames() { return columnNames; }

    public List<Row> rows() { return rows; }

    public Schema schema() {
        List<Column> cols = new ArrayList<>();
        for (String name : columnNames) {
            // 基表列允许为 NULL（不额外维护 NOT NULL 约束）
            cols.add(new Column(qualifier, name, true));
        }
        return new Schema(cols);
    }

    /** 从 JSON 请求构造表：{name, alias?, columns:[...], rows:[[...]]}。 */
    public static Table fromJson(Map<String, Object> def) {
        String name = Json.getStr(def, "name");
        String alias = Json.getStr(def, "alias");
        String qualifier = alias != null ? alias : name;
        List<String> columns = new ArrayList<>();
        for (Object c : Json.arr(def.get("columns"))) columns.add(c.toString());
        List<Row> rows = new ArrayList<>();
        for (Object rowObj : Json.arr(def.get("rows"))) {
            List<Object> raw = Json.arr(rowObj);
            if (raw.size() != columns.size()) {
                throw new EngineException("表 " + name + " 某行列数不匹配");
            }
            rows.add(new Row(coerceValues(raw)));
        }
        return new Table(qualifier, columns, rows);
    }

    static List<Object> coerceValues(List<Object> raw) {
        List<Object> out = new ArrayList<>(raw.size());
        for (Object v : raw) out.add(coerce(v));
        return out;
    }

    static Object coerce(Object v) {
        if (v instanceof Integer i) return i.longValue();
        return v;
    }
}
