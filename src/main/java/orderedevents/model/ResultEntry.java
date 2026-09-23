package orderedevents.model;

import java.util.Map;

/**
 * One entry delivered to a partition's ordered output. Entries are appended
 * exactly in input sequence order. A cancelled event produces no entry (its
 * sequence number is skipped, visible as a gap in {@code seq}).
 */
public record ResultEntry(
        String id,
        long seq,
        EventState status,          // SUCCEEDED, FAILED or TIMED_OUT (failure placeholders)
        Object value,               // success value, or null
        String error,               // placeholder detail, or null
        int attempts,               // attempts actually performed
        long durationMillis,        // enqueue -> terminal settlement
        long committedAtMillis      // wall-clock time of in-order commit
) {

    public Map<String, Object> toMap() {
        java.util.LinkedHashMap<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("id", id);
        m.put("seq", seq);
        m.put("status", status.name());
        if (value != null) {
            m.put("value", value);
        }
        if (error != null) {
            m.put("error", error);
        }
        m.put("attempts", attempts);
        m.put("durationMillis", durationMillis);
        m.put("committedAtMillis", committedAtMillis);
        return m;
    }
}
