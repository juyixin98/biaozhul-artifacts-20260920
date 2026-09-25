package dev.example.cp.core;

import java.util.Map;

/**
 * 一条输入事件。
 *
 * @param offset 事件在输入分区中的偏移（本参考实现中即行号，从 0 开始）
 * @param key    分组键（keyed 算子按它聚合）
 * @param value  整数值（示例算子对它求和）
 */
public record Event(long offset, String key, long value) {

    public static Event of(long offset, String key, long value) {
        return new Event(offset, key, value);
    }

    /** 从 JSON 对象构造，形如 {"key":"a","value":1,"offset":0}；offset 缺省由调用方补。 */
    @SuppressWarnings("unchecked")
    public static Event fromJson(Map<String, Object> json, long defaultOffset) {
        String key;
        Object k = json.get("key");
        if (k == null) {
            throw new IllegalArgumentException("event missing 'key'");
        }
        key = k.toString();
        Object v = json.get("value");
        if (!(v instanceof Number)) {
            throw new IllegalArgumentException("event 'value' must be a number");
        }
        long offset = json.containsKey("offset") && json.get("offset") instanceof Number n
                ? n.longValue() : defaultOffset;
        return new Event(offset, key, ((Number) v).longValue());
    }
}
