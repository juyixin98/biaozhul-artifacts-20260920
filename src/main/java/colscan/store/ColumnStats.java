package colscan.store;

import colscan.json.Json;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一个列分片的统计信息：NULL 计数 + 非 NULL 值的 min/max。
 * 全 NULL 列时 min/max 为 null（注意：是统计意义上的“无界”，不是 0）。
 */
public final class ColumnStats {

    public final long nullCount;
    public final Number min; // 全 NULL 列时为 null
    public final Number max;

    public ColumnStats(long nullCount, Number min, Number max) {
        this.nullCount = nullCount;
        this.min = min;
        this.max = max;
    }

    public boolean allNull() {
        return min == null;
    }

    @SuppressWarnings("unchecked")
    public static ColumnStats fromJson(Map<String, Object> m) {
        long nullCount = ((Number) m.get("nullCount")).longValue();
        Object mn = m.get("min");
        Object mx = m.get("max");
        return new ColumnStats(nullCount,
                mn == null || mn == Json.NULL ? null : (Number) mn,
                mx == null || mx == Json.NULL ? null : (Number) mx);
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("nullCount", nullCount);
        m.put("min", min);
        m.put("max", max);
        return m;
    }
}
