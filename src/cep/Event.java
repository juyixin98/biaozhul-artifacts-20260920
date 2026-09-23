package cep;

import java.util.Objects;

/**
 * 单个输入事件。
 *
 * 时间戳单位为毫秒，语义是“事件时间”。同一实体（entityId）下，
 * 事件必须按非递减时间戳送入引擎；时间戳完全相同的事件，按其到达
 * （输入）先后次序处理，用全局单调递增的 seq（输入序号）破并。
 */
public final class Event {

    /** 事件类型，匹配引擎只识别 A / B / C，其余类型一律视为无关事件并跳过。 */
    public final String type;

    /** 实体标识，模式 A->B->C 在实体内部匹配，不同实体之间绝不串配。 */
    public final String entityId;

    /** 事件时间，epoch 毫秒。 */
    public final long timestamp;

    /**
     * 全局输入序号，由引擎在接收批次时统一分配。
     * 时间戳相同时，seq 小的事件视为先发生。
     * -1 表示尚未分配序号（调用方构造时无需填写）。
     */
    public final long seq;

    public Event(String type, String entityId, long timestamp, long seq) {
        this.type = type;
        this.entityId = entityId;
        this.timestamp = timestamp;
        this.seq = seq;
    }

    /** 返回带输入序号的副本，其余字段保持不变。 */
    public Event withSeq(long newSeq) {
        return new Event(type, entityId, timestamp, newSeq);
    }

    public boolean isA() {
        return "A".equals(type);
    }

    public boolean isB() {
        return "B".equals(type);
    }

    public boolean isC() {
        return "C".equals(type);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Event)) return false;
        Event event = (Event) o;
        return timestamp == event.timestamp
                && seq == event.seq
                && Objects.equals(type, event.type)
                && Objects.equals(entityId, event.entityId);
    }

    @Override
    public int hashCode() {
        return Objects.hash(type, entityId, timestamp, seq);
    }

    @Override
    public String toString() {
        return "Event{" + type + ", entity=" + entityId
                + ", ts=" + timestamp + ", seq=" + seq + '}';
    }
}
