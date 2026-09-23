package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 内存中的一张基表：数据行 + 列名 + 基础统计。 */
public final class Table {
    public final String name;
    public final List<Map<String, Object>> rows = new ArrayList<>();
    public final List<String> columns = new ArrayList<>();
    /** 请求中显式给出的统计（优化器使用；为空时从数据推导）。 */
    public Stats providedStats;
    /** 从实际数据计算的统计（执行器使用，也用于展示估计/实际偏差）。 */
    public Stats actualStats;

    public Table(String name) {
        this.name = name;
    }

    /** 从实际数据计算行数与各列 NDV（仅对字符串/整数/布尔等可比较值计数）。 */
    public Stats computeStatsFromData() {
        Map<String, Map<Object, Boolean>> distinct = new LinkedHashMap<>();
        for (String c : columns) distinct.put(c, new LinkedHashMap<>());
        for (Map<String, Object> row : rows) {
            for (Map.Entry<String, Object> e : row.entrySet()) {
                distinct.get(e.getKey()).put(e.getValue(), Boolean.TRUE);
            }
        }
        Stats s = new Stats(rows.size());
        for (Map.Entry<String, Map<Object, Boolean>> e : distinct.entrySet()) {
            s.ndv.put(ColumnRef.of(name, e.getKey()).canonical, (double) e.getValue().size());
        }
        actualStats = s;
        return s;
    }
}
