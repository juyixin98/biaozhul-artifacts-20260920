package windowengine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 测试数据构造小工具：用链式 API 拼 schema/数据，并生成对应的请求 JSON 与
 * 参考实现输入，保证两边看到完全相同的数据。
 */
public final class TestData {

    public final List<String> columnNames = new ArrayList<>();
    public final List<Value.Type> columnTypes = new ArrayList<>();
    public final List<Object[]> rows = new ArrayList<>(); // Long/String/null
    private final Map<String, Integer> index = new LinkedHashMap<>();

    public TestData column(String name, Value.Type type) {
        if (index.containsKey(name.toLowerCase())) {
            throw new IllegalArgumentException("重复列: " + name);
        }
        index.put(name.toLowerCase(), columnNames.size());
        columnNames.add(name);
        columnTypes.add(type);
        return this;
    }

    public TestData row(Object... values) {
        if (values.length != columnNames.size()) {
            throw new IllegalArgumentException("列数不符");
        }
        rows.add(values);
        return this;
    }

    public int col(String name) {
        Integer i = index.get(name.toLowerCase());
        if (i == null) {
            throw new IllegalArgumentException("无此列: " + name);
        }
        return i;
    }

    public Object get(int row, String name) {
        return rows.get(row)[col(name)];
    }

    public int size() {
        return rows.size();
    }

    /** 构造内存关系（走正式 Schema/Row 路径）。 */
    public Relation toRelation() {
        Schema schema = new Schema(columnNames, columnTypes);
        List<Row> rs = new ArrayList<>();
        for (int r = 0; r < rows.size(); r++) {
            Value[] cells = new Value[schema.size()];
            for (int c = 0; c < schema.size(); c++) {
                cells[c] = toValue(rows.get(r)[c]);
            }
            rs.add(new Row(cells, r));
        }
        return new Relation(schema, rs);
    }

    static Value toValue(Object o) {
        if (o == null) {
            return Value.NULL;
        }
        if (o instanceof Long l) {
            return Value.ofLong(l);
        }
        if (o instanceof Integer i) {
            return Value.ofLong(i.longValue());
        }
        if (o instanceof String s) {
            return Value.ofString(s);
        }
        throw new IllegalArgumentException("不支持的测试值: " + o.getClass());
    }

    /**
     * 组装成请求 JSON 树。
     *
     * @param windowNode window 对象（partitionBy/orderBy/frame）
     * @param functions  函数定义
     */
    public Map<String, Object> toRequest(Map<String, Object> windowNode,
                                         List<Map<String, Object>> functions) {
        List<Object> cols = new ArrayList<>();
        for (int i = 0; i < columnNames.size(); i++) {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("name", columnNames.get(i));
            c.put("type", columnTypes.get(i).name());
            cols.add(c);
        }
        List<Object> rowData = new ArrayList<>();
        for (Object[] r : rows) {
            List<Object> cells = new ArrayList<>();
            for (Object v : r) {
                cells.add(v); // Long / String / null 直接对应 JSON
            }
            rowData.add(cells);
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", cols);
        data.put("rows", rowData);

        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("window", windowNode);
        plan.put("functions", functions);

        Map<String, Object> request = new LinkedHashMap<>();
        request.put("data", data);
        request.put("plan", plan);
        return request;
    }
}
