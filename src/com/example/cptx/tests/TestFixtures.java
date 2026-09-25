package com.example.cptx.tests;

import com.example.cptx.core.Event;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 确定性测试数据：37 条事件，A/B/C 三个键，值取 0.5 的倍数（double 可精确求和）。 */
public final class TestFixtures {

    public static final int N = 37;

    private TestFixtures() {}

    public static List<Map<String, Object>> payloads() {
        return payloads(N);
    }

    public static List<Map<String, Object>> payloads(int n) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            Map<String, Object> e = new LinkedHashMap<>();
            String[] keys = {"A", "B", "C"};
            e.put("key", keys[i % 3]);
            e.put("value", 0.5 * (i % 5)); // 0,0.5,1.0,1.5,2.0 循环
            out.add(e);
        }
        return out;
    }

    /** 独立的小数据参考实现：直接对事件列表做键控 count/sum，不经过被测管线。 */
    public static Map<String, Double> referenceSums(int n) {
        Map<String, Double> sums = new LinkedHashMap<>();
        Map<String, Long> counts = new LinkedHashMap<>();
        for (Event e : events(n)) {
            sums.merge(e.key, e.value, Double::sum);
            counts.merge(e.key, 1L, Long::sum);
        }
        return sums;
    }

    public static long expectedCount(int n, String key) {
        long c = 0;
        for (Event e : events(n)) if (e.key.equals(key)) c++;
        return c;
    }

    public static List<Event> events(int n) {
        List<Event> out = new ArrayList<>();
        String[] keys = {"A", "B", "C"};
        for (int i = 0; i < n; i++) {
            out.add(new Event(i, keys[i % 3], 0.5 * (i % 5)));
        }
        return out;
    }
}
