package com.example.sessionwindow;

import java.util.Map;
import java.util.Objects;

/**
 * One incoming keypress event.
 *
 * @param key     grouping key (e.g. user/device id)
 * @param ts      event time; unit is abstract (the acceptance tests use integer time)
 * @param clientId optional client-assigned idempotency id; same id is processed once
 * @param payload optional free-form payload stored with the session
 */
public record Event(String key, long ts, String clientId, String payload) {

    public Event {
        Objects.requireNonNull(key, "key");
        if (key.isEmpty()) {
            throw new IllegalArgumentException("key must not be empty");
        }
    }

    public static Event fromMap(Map<String, Object> m) {
        String key = Json.requireStr(m, "key");
        long ts;
        Object tsRaw = m.get("ts");
        if (tsRaw == null) {
            // allow "timestamp" as an alias
            tsRaw = m.get("timestamp");
        }
        if (!(tsRaw instanceof Number n)) {
            throw new IllegalArgumentException("Field 'ts' is required and must be a number");
        }
        ts = n.longValue();
        String clientId = Json.str(m, "clientId");
        if (clientId != null && clientId.isEmpty()) {
            clientId = null;
        }
        Object p = m.get("payload");
        String payload = p == null ? null : String.valueOf(p);
        return new Event(key, ts, clientId, payload);
    }

    public Map<String, Object> toLogMap() {
        // record written to event-log.jsonl
        var m = new java.util.LinkedHashMap<String, Object>();
        m.put("type", "event");
        m.put("key", key);
        m.put("ts", ts);
        if (clientId != null) {
            m.put("clientId", clientId);
        }
        if (payload != null) {
            m.put("payload", payload);
        }
        return m;
    }

    @SuppressWarnings("unchecked")
    public static Event fromLogMap(Map<String, Object> m) {
        return new Event(
                Json.requireStr(m, "key"),
                ((Number) m.get("ts")).longValue(),
                m.get("clientId") == null ? null : String.valueOf(m.get("clientId")),
                m.get("payload") == null ? null : String.valueOf(m.get("payload")));
    }
}
