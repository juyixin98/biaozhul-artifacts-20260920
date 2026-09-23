package colscan.query;

import colscan.json.Json;
import colscan.store.Catalog;
import colscan.store.Types;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 校验后的查询请求。 */
public final class QueryRequest {

    public String table;
    public Filter filter;            // 可空
    public final List<AggSpec> aggs = new java.util.ArrayList<>();
    public boolean returnRows;
    public int rowLimit = 100;

    // 执行前由引擎填充
    public Map<String, String> schema = new LinkedHashMap<>();
    public int shardCount;

    /**
     * 解析并校验 HTTP 请求体。
     * <pre>
     * {
     *   "table": "sales",
     *   "filter": {"column":"amount","op":"ge","value":100},
     *   "aggregates": [{"alias":"cnt","func":"count"},
     *                  {"alias":"s","func":"sum","column":"amount"}],
     *   "returnRows": true,
     *   "rowLimit": 100
     * }
     * </pre>
     */
    @SuppressWarnings("unchecked")
    public static QueryRequest parse(Map<String, Object> body, Catalog catalog) throws Exception {
        QueryRequest req = new QueryRequest();
        Object tableObj = body.get("table");
        if (!(tableObj instanceof String)) {
            throw new IllegalArgumentException("缺少 table 字段（字符串）");
        }
        req.table = (String) tableObj;

        Catalog.Table table = catalog.getTable(req.table);
        if (table == null) {
            throw new IllegalArgumentException("表不存在: " + req.table);
        }
        req.schema = table.columns;
        req.shardCount = table.shardCount;
        if (table.shardCount == 0) {
            throw new IllegalArgumentException("表 " + req.table + " 还没有任何分片");
        }

        Object filterObj = body.get("filter");
        if (filterObj != null && filterObj != Json.NULL) {
            if (!(filterObj instanceof Map)) {
                throw new IllegalArgumentException("filter 必须是对象");
            }
            Map<String, Object> fm = (Map<String, Object>) filterObj;
            String col = Json.asString(fm.get("column"));
            String op = Json.asString(fm.get("op"));
            Object valueObj = fm.get("value");
            if (col == null || op == null) {
                throw new IllegalArgumentException("filter 需要 column 与 op");
            }
            if (!table.columns.containsKey(col)) {
                throw new IllegalArgumentException("filter 引用了不存在的列: " + col);
            }
            if (!Filter.isValidOp(op)) {
                throw new IllegalArgumentException("不支持的 op: " + op
                        + "（支持 eq/ne/lt/le/gt/ge）");
            }
            if (!(valueObj instanceof Number)) {
                throw new IllegalArgumentException(
                        "filter.value 必须是数字（当前仅支持数值列），实际为: "
                                + typeName(valueObj));
            }
            req.filter = new Filter(col, op, (Number) valueObj);
        }

        Object aggsObj = body.get("aggregates");
        if (aggsObj != null && aggsObj != Json.NULL) {
            if (!(aggsObj instanceof List)) {
                throw new IllegalArgumentException("aggregates 必须是数组");
            }
            java.util.Set<String> aliases = new java.util.HashSet<>();
            for (Object item : (List<Object>) aggsObj) {
                if (!(item instanceof Map)) {
                    throw new IllegalArgumentException("aggregates 元素必须是对象");
                }
                Map<String, Object> am = (Map<String, Object>) item;
                String func = Json.asString(am.get("func"));
                if (func == null || !AggSpec.isValidFunc(func)) {
                    throw new IllegalArgumentException(
                            "aggregate.func 非法（支持 count/count_col/sum/avg/min/max）: "
                                    + func);
                }
                String col = Json.asString(am.get("column"));
                if (!func.equals(AggSpec.COUNT)) {
                    if (col == null) {
                        throw new IllegalArgumentException(
                                "聚合 " + func + " 需要 column（count 即 COUNT(*) 除外）");
                    }
                    if (!table.columns.containsKey(col)) {
                        throw new IllegalArgumentException("聚合引用了不存在的列: " + col);
                    }
                }
                String alias = Json.asString(am.get("alias"));
                if (alias == null || alias.isBlank()) {
                    alias = func + (col == null ? "" : "_" + col);
                }
                if (!aliases.add(alias)) {
                    throw new IllegalArgumentException("重复的聚合别名: " + alias);
                }
                req.aggs.add(new AggSpec(alias, func, col));
            }
        }

        Object rowsObj = body.get("returnRows");
        req.returnRows = Boolean.TRUE.equals(rowsObj)
                || (rowsObj == null && req.aggs.isEmpty()); // 未给聚合时默认返回样例行

        Object limitObj = body.get("rowLimit");
        if (limitObj instanceof Number) {
            req.rowLimit = ((Number) limitObj).intValue();
            if (req.rowLimit < 1 || req.rowLimit > 10000) {
                throw new IllegalArgumentException("rowLimit 必须在 1..10000");
            }
        }

        if (!req.returnRows && req.aggs.isEmpty()) {
            throw new IllegalArgumentException("returnRows=false 时必须提供 aggregates");
        }
        return req;
    }

    private static String typeName(Object v) {
        if (v == null || v == Json.NULL) return "null";
        if (v instanceof String) return "string";
        if (v instanceof Boolean) return "boolean";
        return v.getClass().getSimpleName();
    }
}
