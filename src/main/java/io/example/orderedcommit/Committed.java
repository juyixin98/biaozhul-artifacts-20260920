package io.example.orderedcommit;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * One entry of a partition's committed output. The committed list is strictly
 * ordered by {@code seq}, which is the guarantee of the service: an event's
 * result (or failure placeholder) is appended only after every earlier event
 * has been resolved.
 */
final class Committed {
    final long seq;
    final String eventId;
    final boolean success;
    final Object result;
    final String error;
    final int attempts;
    final long completedAt;
    final long committedAt;

    Committed(Event event, boolean success) {
        this.seq = event.seq;
        this.eventId = event.id;
        this.success = success;
        this.result = success ? event.result : null;
        this.error = success ? null : event.lastError;
        this.attempts = event.attempts;
        this.completedAt = event.finishedAt;
        this.committedAt = event.committedAt;
    }

    Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("seq", seq);
        m.put("eventId", eventId);
        m.put("outcome", success ? "SUCCESS" : "FAILURE");
        if (success) {
            m.put("result", result);
        } else {
            m.put("error", error);
        }
        m.put("attempts", attempts);
        m.put("completedAt", completedAt);
        m.put("committedAt", committedAt);
        return m;
    }
}
