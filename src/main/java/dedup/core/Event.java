package dedup.core;

import dedup.json.Json;

import java.time.Instant;
import java.time.format.DateTimeParseException;

/**
 * 一个待处理事件。
 *
 * <p><b>关键设计：事件 ID 与事件时间分离。</b>
 * {@code id} 是事件身份（配合可选的 {@code key} 分区），{@code eventTime} 是事件自带的
 * 业务时间戳，二者相互独立。乱序到达的事件可能携带更早的事件时间，去重判断完全基于
 * 事件时间与水位线，而不依赖不可靠的处理机器时钟（见 {@code ClockFallbackTest}）。
 *
 * @param key       可选的分区/流键（null 或空串表示默认键 "_default"）
 * @param id        事件 ID（非空），同一 (key,id) 被视为同一事件的副本
 * @param eventTime 事件时间（epoch 毫秒）
 * @param payload   原始载荷（用于“同 ID 不同载荷”检测；可为 {@link Json.Nul}）
 */
public record Event(String key, String id, long eventTime, Json.Value payload) {

    public Event {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("event.id 不允许为空");
        }
        key = (key == null || key.isEmpty()) ? "_default" : key;
        if (payload == null) {
            payload = Json.Nul.INSTANCE;
        }
    }

    /** 去重身份键（复合键）。 */
    public CompositeKey compositeKey() {
        return new CompositeKey(key, id);
    }

    /**
     * 从 JSON 对象构造事件：
     * <pre>{"id": "e1", "key": "user-a", "eventTime": 1000, "payload": {...}}</pre>
     * eventTime 允许是整数毫秒，或 ISO-8601 字符串（如 "2026-09-23T10:00:00Z"）。
     */
    public static Event fromJson(Json.Obj obj) {
        String id = Json.requireStr(obj.get("id"), "id");
        String key = obj.getStr("key");
        Json.Value tv = obj.get("eventTime");
        if (tv == null) {
            throw new Json.JsonException("缺少字段 eventTime");
        }
        long eventTime;
        if (tv instanceof Json.Num) {
            eventTime = Json.longExact(tv, "eventTime");
        } else if (tv instanceof Json.Str s) {
            try {
                eventTime = Instant.parse(s.value()).toEpochMilli();
            } catch (DateTimeParseException e) {
                throw new Json.JsonException("eventTime 必须是整数毫秒或 ISO-8601 时间: " + s.value());
            }
        } else {
            throw new Json.JsonException("eventTime 必须是整数毫秒或 ISO-8601 字符串");
        }
        Json.Value payload = obj.getOrDefault("payload", Json.Nul.INSTANCE);
        return new Event(key, id, eventTime, payload);
    }

    /** 序列化为 JSON 对象（保持字段顺序）。 */
    public Json.Obj toJson() {
        Json.Obj o = new Json.Obj();
        o.put("key", new Json.Str(key));
        o.put("id", new Json.Str(id));
        o.put("eventTime", Json.Num.of(eventTime));
        o.put("payload", payload);
        return o;
    }

    public String payloadHash() {
        return Json.sha256Canonical(payload);
    }
}
