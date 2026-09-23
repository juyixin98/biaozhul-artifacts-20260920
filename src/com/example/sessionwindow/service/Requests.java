package com.example.sessionwindow.service;

import com.example.sessionwindow.json.Json;
import com.example.sessionwindow.json.JsonException;
import com.example.sessionwindow.model.Event;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Parses service request bodies into typed objects. */
public final class Requests {

    private Requests() {
    }

    public static ServiceConfig parseConfig(Map<String, Object> body) {
        long gap = Json.lng(body, "gap", 10L);
        long allowedLateness = Json.lng(body, "allowedLateness", 0L);
        boolean autoWatermark = Json.bool(body, "autoWatermark", true);
        long outOfOrderness = Json.lng(body, "outOfOrderness", 0L);
        if (gap <= 0) {
            throw new IllegalArgumentException("gap must be positive");
        }
        if (allowedLateness < 0 || outOfOrderness < 0) {
            throw new IllegalArgumentException("allowedLateness and outOfOrderness must be >= 0");
        }
        return new ServiceConfig(gap, allowedLateness, autoWatermark, outOfOrderness);
    }

    /**
     * Parse the ordered {@code items} array. Each item is either:
     * <ul>
     *   <li>{"key":"u1","timestamp":3,"value":2.5} - an event (value defaults to 1.0)</li>
     *   <li>{"watermark":7} - a watermark marker</li>
     * </ul>
     * A plain {@code events} array is accepted too.
     */
    public static List<BatchProcessor.Item> parseItems(Map<String, Object> body) {
        List<Object> rawItems = Json.list(body, "items");
        List<Object> rawEvents = Json.list(body, "events");
        if (!rawItems.isEmpty() && !rawEvents.isEmpty()) {
            throw new IllegalArgumentException("use either 'items' or 'events', not both");
        }

        List<BatchProcessor.Item> items = new ArrayList<>();
        if (!rawItems.isEmpty()) {
            for (Object o : rawItems) {
                items.add(parseItem(o));
            }
        } else {
            for (Object o : rawEvents) {
                items.add(BatchProcessor.Item.event(parseEvent(o)));
            }
        }
        return items;
    }

    private static BatchProcessor.Item parseItem(Object o) {
        if (!(o instanceof Map<?, ?> raw)) {
            throw new JsonException("each item must be an object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) raw;
        if (m.containsKey("watermark")) {
            return BatchProcessor.Item.watermark(Json.lng(m, "watermark", 0L));
        }
        return BatchProcessor.Item.event(parseEvent(m));
    }

    @SuppressWarnings("unchecked")
    static Event parseEvent(Object o) {
        if (!(o instanceof Map<?, ?>)) {
            throw new JsonException("event must be an object");
        }
        Map<String, Object> m = (Map<String, Object>) o;
        if (!m.containsKey("key") || !(m.get("key") instanceof String key)) {
            throw new JsonException("event.key must be a string");
        }
        if (!m.containsKey("timestamp") || !(m.get("timestamp") instanceof Number)) {
            throw new JsonException("event.timestamp must be a number");
        }
        long timestamp = ((Number) m.get("timestamp")).longValue();
        double value = Json.dbl(m, "value", 1.0);
        return Event.of(key, timestamp, value);
    }
}
