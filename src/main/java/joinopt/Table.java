package joinopt;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 输入表定义：表名、列名、行数（统计）以及可选的实际数据行。
 *
 * 统计信息：
 *   - rowCount：基数（行数）。有数据时以实际数据为准（声明值仅用于告警）。
 *   - ndv：每列的不同值个数（distinct values）。未声明时：
 *       * 有数据  -> 按实际数据计算
 *       * 无数据  -> 假设为列的主键，即 ndv = rowCount（均匀假设）
 *
 * 无 rows 的表为“仅统计”表，可用于优化但无法实际执行。
 */
public final class Table {

    public final String name;
    public final List<String> columns;
    public long rowCount;
    /** 列名 -> 该列不同值个数；顺序与 columns 一致（用 LinkedHashMap 保存）。 */
    public final Map<String, Long> ndv = new LinkedHashMap<>();
    /** 实际数据行。未提供 rows 时为空（此时 statsOnly=true）；rows:[] 表示 0 行真实表。 */
    public final List<List<Object>> rows = new ArrayList<>();
    /** true 表示请求中未提供 rows（仅有统计）；显式 rows:[] 是 0 行真实表，此值为 false。 */
    public boolean statsOnly;

    public Table(String name, List<String> columns) {
        this.name = name;
        this.columns = columns;
    }

    public int colIndex(String column) {
        int i = columns.indexOf(column);
        if (i < 0) {
            throw new IllegalArgumentException("表 " + name + " 中不存在列 " + column
                    + "（可用列: " + columns + "）");
        }
        return i;
    }

    /** 列的全限定名，如 "orders.cust_id"。 */
    public String qualified(int colIdx) {
        return name + "." + columns.get(colIdx);
    }

    /**
     * 从 JSON 构造表。JSON 形如：
     * <pre>
     * { "name": "orders",
     *   "columns": ["id","cust_id"],
     *   "rowCount": 10000,
     *   "ndv": {"id": 10000, "cust_id": 1000},
     *   "rows": [[1,1],[2,1]] }
     * </pre>
     * ndv 也可写成数组（与 columns 对齐）："ndv": [10000, 1000]
     */
    @SuppressWarnings("unchecked")
    public static Table fromJson(Map<String, Object> json, List<String> warnings) {
        String name = Json.str(json, "name");
        if (name == null || name.isEmpty()) throw new IllegalArgumentException("表定义缺少 name");

        List<Object> colsJson = Json.arr(json, "columns");
        if (colsJson == null || colsJson.isEmpty()) {
            throw new IllegalArgumentException("表 " + name + " 缺少 columns 定义");
        }
        List<String> columns = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Object c : colsJson) {
            String col = String.valueOf(c);
            if (!seen.add(col)) throw new IllegalArgumentException("表 " + name + " 的列名重复: " + col);
            columns.add(col);
        }
        Table t = new Table(name, columns);

        // rows 缺省 -> 仅统计表；rows 显式给出（含空数组）-> 有数据的真实表（0 行也算）
        List<Object> rowsJson = Json.arr(json, "rows");
        boolean hasRows = json.containsKey("rows");
        if (rowsJson != null) {
            for (Object r : rowsJson) {
                List<Object> row = Json.asArr(r);
                if (row.size() != columns.size()) {
                    throw new IllegalArgumentException("表 " + name + " 的数据行列数(" + row.size()
                            + ")与列数(" + columns.size() + ")不一致");
                }
                t.rows.add(new ArrayList<>(row));
            }
        }
        t.statsOnly = !hasRows;

        long declared = json.containsKey("rowCount") ? Json.lng(json, "rowCount") : -1;
        if (hasRows) {
            t.rowCount = t.rows.size();
            if (declared >= 0 && declared != t.rowCount) {
                warnings.add("表 " + name + " 声明 rowCount=" + declared
                        + " 与实际数据行数 " + t.rowCount + " 不一致，已以实际数据为准");
            }
        } else {
            if (declared < 0) {
                throw new IllegalArgumentException("仅统计表 " + name + " 必须提供 rowCount");
            }
            t.rowCount = declared;
        }
        if (t.rowCount < 0) throw new IllegalArgumentException("表 " + name + " 的 rowCount 为负");

        // NDV：对象形式、数组形式或缺省
        Object ndvJson = json.get("ndv");
        Map<String, Long> ndvMap = new LinkedHashMap<>();
        if (ndvJson instanceof Map) {
            Map<String, Object> mj = (Map<String, Object>) ndvJson;
            for (String col : columns) {
                if (!mj.containsKey(col)) {
                    throw new IllegalArgumentException("表 " + name + " 的 ndv 缺少列 " + col);
                }
                long d = Json.asLong(mj.get(col));
                if (d < 0) throw new IllegalArgumentException("表 " + name + "." + col + " 的 ndv 为负");
                ndvMap.put(col, d);
            }
        } else if (ndvJson instanceof List) {
            List<Object> arr = (List<Object>) ndvJson;
            if (arr.size() != columns.size()) {
                throw new IllegalArgumentException("表 " + name + " 的 ndv 数组长度与列数不一致");
            }
            for (int i = 0; i < columns.size(); i++) {
                long d = Json.asLong(arr.get(i));
                if (d < 0) throw new IllegalArgumentException("表 " + name + "." + columns.get(i) + " 的 ndv 为负");
                ndvMap.put(columns.get(i), d);
            }
        } else {
            // 未声明：有数据算实际 NDV；无数据假设为主键列 ndv = rowCount
            for (int i = 0; i < columns.size(); i++) {
                long d;
                if (!t.statsOnly) {
                    d = countDistinct(t.rows, i);
                } else {
                    d = t.rowCount;
                }
                ndvMap.put(columns.get(i), d);
            }
            if (!t.statsOnly) {
                warnings.add("表 " + name + " 未提供 ndv，已按实际数据计算不同值个数");
            }
        }

        // NDV 不能超过行数（0 行表 NDV 强制为 0）
        for (String col : columns) {
            long d = ndvMap.get(col);
            long capped = t.rowCount == 0 ? 0 : Math.min(d, t.rowCount);
            if (d != capped) {
                warnings.add("表 " + name + "." + col + " 声明 ndv=" + d
                        + " 超过行数 " + t.rowCount + "，已截断为 " + capped);
            }
            t.ndv.put(col, capped);
        }
        return t;
    }

    /** 统计第 colIdx 列的不同值个数；null 单独计为一个值。 */
    public static long countDistinct(List<List<Object>> rows, int colIdx) {
        Set<String> set = new HashSet<>();
        for (List<Object> row : rows) {
            set.add(canon(row.get(colIdx)));
        }
        return set.size();
    }

    /**
     * 值的规范化字符串：作为哈希键与不同值计数的统一依据。
     * 整数 1 与浮点 1.0 视为相等（JSON 中均为数值类型），字符串与数字不混同。
     * null 用保留前缀表示，普通字符串加 "s:" 前缀避免与数字撞键。
     */
    public static String canon(Object v) {
        if (v == null) return "#null";
        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (d == Math.rint(d) && !Double.isInfinite(d)) {
                return "n:" + (long) d;
            }
            return "n:" + d;
        }
        if (v instanceof Number) return "n:" + ((Number) v).longValue();
        if (v instanceof Boolean) return "b:" + v;
        return "s:" + v;
    }

    @Override
    public String toString() {
        return name + "(" + rowCount + "行)";
    }
}
