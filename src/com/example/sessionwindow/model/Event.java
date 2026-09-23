package com.example.sessionwindow.model;

import java.util.Map;

/**
 * One input event.
 *
 * @param key       partitioning key (sessions are formed independently per key)
 * @param timestamp event time, in milliseconds
 * @param value     numeric contribution to the aggregate; missing values default to 1.0
 */
public record Event(String key, long timestamp, double value) {

    public static Event of(String key, long timestamp) {
        return new Event(key, timestamp, 1.0);
    }

    public static Event of(String key, long timestamp, double value) {
        return new Event(key, timestamp, value);
    }

    /** JSON object form: {"key":"u1","timestamp":3,"value":2.5}. */
    public Map<String, Object> toJson() {
        return Map.of("key", key, "timestamp", timestamp, "value", value);
    }
}
