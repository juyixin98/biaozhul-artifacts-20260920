package streammatch.model;

/**
 * 按键(key)事件流中的一个输入事件。
 *
 * @param id        事件唯一 ID（由调用方保证；服务层会校验非空且不重复）
 * @param key       分区键；不同 key 之间互不影响（A 等待 B 时不会跨 key 匹配）
 * @param type      事件类型：仅允许 A / B / C（大小写敏感）
 * @param timestamp 事件时间（epoch 毫秒，可为任意整数，包括 0 与负数）
 * @param seq       到达序号：由引擎按输入顺序分配，用于定义同一时间事件的全序
 *                  （见 {@code (timestamp, seq)} 排序规则）
 */
public record Event(String id, String key, String type, long timestamp, long seq) {

    public static final String A = "A";
    public static final String B = "B";
    public static final String C = "C";

    public Event {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("event id must not be empty");
        }
        if (key == null || key.isEmpty()) {
            throw new IllegalArgumentException("event key must not be empty");
        }
        if (!A.equals(type) && !B.equals(type) && !C.equals(type)) {
            throw new IllegalArgumentException("event type must be one of A, B, C: " + type);
        }
    }
}
