package com.example.drvb.core;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * An immutable event record.
 *
 * <p>Every event carries:
 * <ul>
 *   <li>{@code id} — client-supplied unique identifier;</li>
 *   <li>{@code type} — event type (e.g. {@code "payment"});</li>
 *   <li>{@code eventTime} — the time the event happened, epoch milliseconds.
 *       <b>All rule binding is performed against this time, never against
 *       processing time.</b></li>
 *   <li>{@code payload} — arbitrary nested attributes addressed by dotted
 *       paths in rule conditions (e.g. {@code "amount"} or
 *       {@code "user.tier"}).</li>
 * </ul>
 */
public final class Event {

    private final String id;
    private final String type;
    private final long eventTime;
    private final Map<String, Object> payload;

    public Event(String id, String type, long eventTime, Map<String, Object> payload) {
        this.id = Objects.requireNonNull(id, "id");
        this.type = Objects.requireNonNull(type, "type");
        this.eventTime = eventTime;
        this.payload = payload == null
                ? Map.of()
                : Collections.unmodifiableMap(new LinkedHashMap<>(payload));
    }

    public String id() {
        return id;
    }

    public String type() {
        return type;
    }

    public long eventTime() {
        return eventTime;
    }

    public Map<String, Object> payload() {
        return payload;
    }
}
