package dedup.core;

import dedup.json.Json;

/**
 * 去重墓碑：记录某个 {@link CompositeKey} 第一次被接受时的信息。
 *
 * @param eventTime 首次事件的事件时间（用于按水位线排序释放）
 * @param seq       首次到达时的单调序列号（硬上限淘汰时的次级排序键）
 * @param payloadHash 首次事件载荷的规范化 SHA-256 指纹（载荷不一致检测）
 */
public record Tombstone(long eventTime, long seq, String payloadHash) {

    public Json.Obj toJson(String key, String id) {
        Json.Obj o = new Json.Obj();
        o.put("key", new Json.Str(key));
        o.put("id", new Json.Str(id));
        o.put("eventTime", Json.Num.of(eventTime));
        o.put("seq", Json.Num.of(seq));
        o.put("payloadHash", new Json.Str(payloadHash));
        return o;
    }

    public static Tombstone fromJson(Json.Obj o) {
        return new Tombstone(
                Json.longExact(o.get("eventTime"), "eventTime"),
                Json.longExact(o.get("seq"), "seq"),
                Json.requireStr(o.get("payloadHash"), "payloadHash"));
    }
}
