package cep.model;

import java.util.Objects;

/**
 * 一次按键事件。
 *
 * <p>事件携带两个时间概念：
 * <ul>
 *   <li>{@code timestamp}：事件时间（event time），模式匹配与超时全部基于它；</li>
 *   <li>{@code seq}：同一事件时间下的次序编号（输入中显式给出，或由引擎按到达顺序补 0..n-1）。</li>
 * </ul>
 *
 * <p>事件的全局全序是 {@code (timestamp asc, seq asc)}，所有"A 在 B 之前""C 位于 A、B 之间"
 * 的判断都基于该全序，因此同一毫秒到达的事件也有确定的先后。
 */
public final class KeyEvent implements Comparable<KeyEvent> {

    private final String id;
    private final String key;
    private final long timestamp;
    private final long seq;
    /** 该事件是否通过迟到尽力车道处理（仅用于输出标记，不影响匹配语义）。 */
    private final boolean late;

    public KeyEvent(String id, String key, long timestamp, long seq) {
        this(id, key, timestamp, seq, false);
    }

    public KeyEvent(String id, String key, long timestamp, long seq, boolean late) {
        this.id = Objects.requireNonNull(id, "event id");
        this.key = Objects.requireNonNull(key, "event key");
        this.timestamp = timestamp;
        this.seq = seq;
        this.late = late;
    }

    public String id() { return id; }
    public String key() { return key; }
    public long timestamp() { return timestamp; }
    public long seq() { return seq; }
    public boolean late() { return late; }

    public KeyEvent withLate(boolean value) {
        return new KeyEvent(id, key, timestamp, seq, value);
    }

    /** 仅按全序比较，不含 late 标记。 */
    @Override
    public int compareTo(KeyEvent o) {
        int c = Long.compare(timestamp, o.timestamp);
        return c != 0 ? c : Long.compare(seq, o.seq);
    }

    @Override
    public String toString() {
        return id + "(" + key + "@" + timestamp + "#" + seq + (late ? ",late" : "") + ")";
    }
}
