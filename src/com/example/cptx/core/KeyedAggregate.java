package com.example.cptx.core;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 键控聚合算子：状态 = 每个 key 的 (count, sum)，外加已处理到的输入偏移由检查点元数据统一保存。
 *
 * 这是带算子状态的算子：process() 推进内存状态；snapshot()/restore() 接入检查点协议。
 * 小数据精确参考实现：double 直接求和（测试样例选用 0.5 倍数等可精确表示的值）。
 */
public final class KeyedAggregate {

    public record Agg(long count, double sum) {
        Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("count", count);
            m.put("sum", sum);
            return m;
        }

        static Agg fromJson(Map<String, Object> m) {
            return new Agg(Json.lng(m.get("count")), Json.dbl(m.get("sum")));
        }
    }

    private final Map<String, Agg> state = new TreeMap<>();

    public void process(Event e) {
        Agg a = state.getOrDefault(e.key, new Agg(0, 0.0));
        state.put(e.key, new Agg(a.count() + 1, a.sum() + e.value));
    }

    /** 深拷贝快照（检查点在状态写入后、屏障对齐前不得再被后续事件改写）。 */
    public synchronized Map<String, Map<String, Object>> snapshot() {
        Map<String, Map<String, Object>> copy = new TreeMap<>();
        for (Map.Entry<String, Agg> e : state.entrySet()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("count", e.getValue().count());
            m.put("sum", e.getValue().sum());
            copy.put(e.getKey(), m);
        }
        return copy;
    }

    public synchronized void restore(Map<String, Map<String, Object>> raw) {
        state.clear();
        for (Map.Entry<String, Map<String, Object>> e : raw.entrySet()) {
            state.put(e.getKey(), Agg.fromJson(e.getValue()));
        }
    }

    public synchronized Map<String, Map<String, Object>> currentView() {
        return snapshot();
    }

    public synchronized List<Map<String, Object>> rows() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map.Entry<String, Agg> e : state.entrySet()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("key", e.getKey());
            row.put("count", e.getValue().count());
            row.put("sum", e.getValue().sum());
            out.add(row);
        }
        return out;
    }
}
