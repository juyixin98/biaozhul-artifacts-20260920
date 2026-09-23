package topk;

/**
 * 一条不可变的计分事件（增量）。
 *
 * <p>事件携带一个分数增量 {@code delta}（可为负），同一个 item 可由多条事件累加。
 * 事件插入后可以被撤回（按 eventId），撤回语义为幂等：重复撤回不会二次扣减。
 */
public final class Event {
    /** 事件唯一 ID（同一分组内唯一）。 */
    public final String eventId;
    /** 归属条目，TopK 的统计对象。 */
    public final String itemId;
    /** 分数增量，可为负。 */
    public final long delta;
    /** 事件时间（逻辑时间戳，毫秒，由调用方提供）。 */
    public final long ts;

    public Event(String eventId, String itemId, long delta, long ts) {
        this.eventId = eventId;
        this.itemId = itemId;
        this.delta = delta;
        this.ts = ts;
    }

    @Override
    public String toString() {
        return "Event{id=" + eventId + ", item=" + itemId + ", delta=" + delta + ", ts=" + ts + "}";
    }
}
