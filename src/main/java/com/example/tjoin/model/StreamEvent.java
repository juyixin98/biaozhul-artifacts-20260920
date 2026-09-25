package com.example.tjoin.model;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Objects;

/**
 * One input record.
 *
 * <p>{@code id} is the unique event identifier: duplicate <em>values</em> are
 * legal (two events may carry the same key, timestamp and payload), but two
 * events never share an id. The operator uses the id for duplicate delivery
 * suppression and for the once-only output guarantee.</p>
 */
public final class StreamEvent {

    /** Unique, non-empty event identifier (unique across both streams). */
    private final String id;

    /** Join key; only events with equal keys can match. */
    private final String key;

    /** Event-time timestamp in milliseconds (domain time, not wall clock). */
    @JsonProperty("eventTime")
    private final long timestamp;

    /** Arbitrary payload kept for output; may be {@code null}. */
    private final Object value;

    public StreamEvent(@JsonProperty("id") String id,
                       @JsonProperty("key") String key,
                       @JsonProperty("eventTime") long timestamp,
                       @JsonProperty("value") Object value) {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("event id must be a non-empty string");
        }
        if (key == null) {
            throw new IllegalArgumentException("event key must not be null (event " + id + ")");
        }
        this.id = id;
        this.key = key;
        this.timestamp = timestamp;
        this.value = value;
    }

    public String getId() {
        return id;
    }

    public String getKey() {
        return key;
    }

    public long getTimestamp() {
        return timestamp;
    }

    public Object getValue() {
        return value;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof StreamEvent that)) {
            return false;
        }
        return timestamp == that.timestamp
                && id.equals(that.id)
                && key.equals(that.key)
                && Objects.equals(value, that.value);
    }

    @Override
    public int hashCode() {
        return Objects.hash(id, key, timestamp, value);
    }

    @Override
    public String toString() {
        return "Event{id='" + id + "', key='" + key + "', t=" + timestamp + ", value=" + value + '}';
    }
}
