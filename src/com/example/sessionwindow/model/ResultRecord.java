package com.example.sessionwindow.model;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * One entry in the result update stream. Updates are expressed as explicit
 * retraction / addition records rather than in-place overwrites:
 *
 * <ul>
 *   <li>{@code ADD}      - provisional result of an active session</li>
 *   <li>{@code RETRACT}  - a previously emitted result (by windowStart/end) is no longer valid</li>
 *   <li>{@code SEALED}   - an ADD result has become final at the watermark</li>
 *   <li>{@code PURGED}   - state for a final session has been deleted (allowedLateness exhausted)</li>
 *   <li>{@code DROPPED}  - an event arrived later than watermark - allowedLateness and was ignored</li>
 *   <li>{@code WATERMARK}- informational marker recording watermark advancement</li>
 * </ul>
 */
public final class ResultRecord {

    public enum Type { ADD, RETRACT, SEALED, PURGED, DROPPED, WATERMARK }

    private final Type type;
    private final String key;
    private final Aggregate aggregate;
    private final Long watermark;
    private final Event event;

    private ResultRecord(Type type, String key, Aggregate aggregate, Long watermark, Event event) {
        this.type = type;
        this.key = key;
        this.aggregate = aggregate;
        this.watermark = watermark;
        this.event = event;
    }

    public static ResultRecord add(String key, Aggregate aggregate) {
        return new ResultRecord(Type.ADD, key, aggregate, null, null);
    }

    public static ResultRecord retract(String key, Aggregate aggregate) {
        return new ResultRecord(Type.RETRACT, key, aggregate, null, null);
    }

    public static ResultRecord sealed(String key, Aggregate aggregate) {
        return new ResultRecord(Type.SEALED, key, aggregate, null, null);
    }

    public static ResultRecord purged(String key, Aggregate aggregate) {
        return new ResultRecord(Type.PURGED, key, aggregate, null, null);
    }

    public static ResultRecord dropped(Event event, long watermark) {
        return new ResultRecord(Type.DROPPED, event.key(), null, watermark, event);
    }

    public static ResultRecord watermark(long watermark) {
        return new ResultRecord(Type.WATERMARK, null, null, watermark, null);
    }

    public Type type() {
        return type;
    }

    public String key() {
        return key;
    }

    public Aggregate aggregate() {
        return aggregate;
    }

    public Long watermark() {
        return watermark;
    }

    public Event event() {
        return event;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type.name());
        switch (type) {
            case WATERMARK -> m.put("watermark", watermark);
            case DROPPED -> {
                m.put("key", key);
                m.put("event", event.toJson());
                m.put("watermark", watermark);
            }
            default -> {
                m.put("key", key);
                m.put("windowStart", aggregate.start());
                m.put("windowEnd", aggregate.end());
                m.put("aggregate", aggregate.toJson());
            }
        }
        return m;
    }

    @Override
    public String toString() {
        return switch (type) {
            case WATERMARK -> "WATERMARK(" + watermark + ")";
            case DROPPED -> "DROPPED(" + event + ", watermark=" + watermark + ")";
            default -> type + "(" + key + ", " + aggregate + ")";
        };
    }
}
