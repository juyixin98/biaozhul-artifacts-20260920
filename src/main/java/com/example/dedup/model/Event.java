package com.example.dedup.model;

import com.example.dedup.json.Json;

/**
 * A stream event.
 *
 * @param id        event identity used for dedup (independent of event time)
 * @param eventTime event time in epoch millis (the stream's logical time)
 * @param type      UPSERT (normal payload) or DELETE (tombstone for key)
 * @param key       business key the event upserts/deletes
 * @param payload   arbitrary JSON payload; may be null (e.g. DELETE)
 */
public record Event(String id, long eventTime, EventType type, String key, Json.Value payload) {

    public enum EventType {
        UPSERT, DELETE
    }

    public static Event upsert(String id, long eventTime, String key, Json.Value payload) {
        return new Event(id, eventTime, EventType.UPSERT, key, payload);
    }

    public static Event delete(String id, long eventTime, String key) {
        return new Event(id, eventTime, EventType.DELETE, key, null);
    }

    public Json.Value toJson() {
        Json.JsonObject o = Json.obj();
        o.members().put("id", Json.str(id));
        o.members().put("eventTime", Json.num(eventTime));
        o.members().put("type", Json.str(type.name()));
        o.members().put("key", Json.str(key));
        if (payload != null) {
            o.members().put("payload", payload);
        }
        return o;
    }

    public static Event fromJson(Json.JsonObject o) {
        String id = o.getString("id", null);
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("event.id is required");
        }
        if (!o.has("eventTime")) {
            throw new IllegalArgumentException("event.eventTime is required");
        }
        long eventTime = o.getLong("eventTime", 0L);
        String typeStr = o.getString("type", "UPSERT").toUpperCase();
        EventType type;
        try {
            type = EventType.valueOf(typeStr);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("unknown event type: " + typeStr);
        }
        String key = o.getString("key", "");
        Json.Value payload = o.get("payload");
        return new Event(id, eventTime, type, key, payload);
    }
}
