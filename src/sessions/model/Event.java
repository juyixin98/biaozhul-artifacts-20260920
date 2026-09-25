package sessions.model;

import java.util.Objects;

/**
 * 不可变的输入事件：某个键在事件时间 {@code timestamp} 上携带一个 long 值。
 */
public final class Event {
    private final String key;
    private final long timestamp;
    private final long value;

    public Event(String key, long timestamp, long value) {
        if (key == null) {
            throw new IllegalArgumentException("event key must not be null");
        }
        this.key = key;
        this.timestamp = timestamp;
        this.value = value;
    }

    public String key() {
        return key;
    }

    public long timestamp() {
        return timestamp;
    }

    public long value() {
        return value;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Event e)) {
            return false;
        }
        return timestamp == e.timestamp && value == e.value && key.equals(e.key);
    }

    @Override
    public int hashCode() {
        return Objects.hash(key, timestamp, value);
    }

    @Override
    public String toString() {
        return "Event{key='" + key + "', ts=" + timestamp + ", value=" + value + '}';
    }
}
