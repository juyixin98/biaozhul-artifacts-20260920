package com.example.cptx.core;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 输入事件。offset 为持久输入日志中的 0 基序列号（= 该事件之前已持久化的事件条数），
 * 与 MapReduce/流处理中的输入偏移语义一致。
 *
 * key/value 是事件载荷：value 为数值，用于按 key 做 count/sum 汇总。
 */
public final class Event {
    public final long offset;
    public final String key;
    public final double value;

    public Event(long offset, String key, double value) {
        if (key == null || key.isBlank()) {
            throw new IllegalArgumentException("事件 key 不能为空");
        }
        if (!Double.isFinite(value)) {
            throw new IllegalArgumentException("事件 value 必须是有限数值");
        }
        this.offset = offset;
        this.key = key;
        this.value = value;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("offset", offset);
        m.put("key", key);
        m.put("value", value);
        return m;
    }

    /** 解析不含 offset 的输入载荷（写入日志时由 InputLog 分配 offset）。 */
    public static Event fromInput(Map<String, Object> m, long offset) {
        Object keyVal = m.get("key");
        Object valueVal = m.get("value");
        if (!(keyVal instanceof String k) || k.isBlank()) {
            throw new IllegalArgumentException("事件缺少非空字符串字段 key");
        }
        if (!(valueVal instanceof Number n)) {
            throw new IllegalArgumentException("事件缺少数值字段 value");
        }
        return new Event(offset, k, n.doubleValue());
    }

    /** 解析日志/API 回显的完整事件（含 offset）。 */
    public static Event fromJson(Map<String, Object> m) {
        Object off = m.get("offset");
        Object keyVal = m.get("key");
        Object valueVal = m.get("value");
        if (!(off instanceof Number o) || !(keyVal instanceof String k) || !(valueVal instanceof Number n)) {
            throw new IllegalArgumentException("事件 JSON 必须包含 offset(数字)/key(字符串)/value(数字)");
        }
        return new Event(o.longValue(), k, n.doubleValue());
    }
}
