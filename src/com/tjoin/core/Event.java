package com.tjoin.core;

import java.util.Objects;

/**
 * 流事件。
 *
 * <p>每个事件携带：唯一 ID、所属流（左/右）、连接键、事件时间戳和任意 JSON 风格的值。
 * 唯一性由 {@code id} 决定（{@code equals}/{@code hashCode} 只基于 id），
 * 因此两个 {@code id} 相同的事件被视为同一事件的重复投递。
 */
public final class Event {

    private final String id;
    private final StreamSide side;
    private final String key;
    private final long timestamp;
    private final Object value;

    public Event(String id, StreamSide side, String key, long timestamp, Object value) {
        this.id = Objects.requireNonNull(id, "event id");
        this.side = Objects.requireNonNull(side, "side");
        this.key = Objects.requireNonNull(key, "key");
        this.timestamp = timestamp;
        this.value = value;
    }

    public String id() {
        return id;
    }

    public StreamSide side() {
        return side;
    }

    public String key() {
        return key;
    }

    public long timestamp() {
        return timestamp;
    }

    public Object value() {
        return value;
    }

    /** 跨流取另一侧。 */
    public StreamSide opposite() {
        return side.opposite();
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Event)) return false;
        return id.equals(((Event) o).id);
    }

    @Override
    public int hashCode() {
        return id.hashCode();
    }

    @Override
    public String toString() {
        return "Event{id=" + id + ", side=" + side + ", key=" + key
                + ", ts=" + timestamp + ", value=" + value + "}";
    }
}
