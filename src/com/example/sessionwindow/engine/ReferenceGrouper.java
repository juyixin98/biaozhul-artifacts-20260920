package com.example.sessionwindow.engine;

import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.Event;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Exact offline reference implementation.
 *
 * <p>Given the <b>complete</b> event stream upfront, sort each key's events by
 * timestamp and cut a new session wherever the gap to the previous event is
 * greater than the inactivity gap. This implements the same half-open boundary
 * rule as {@link SessionWindowEngine} (distance == gap splits) and is used by
 * tests to verify the online, watermark-driven engine produces exactly the
 * right final sessions.</p>
 */
public final class ReferenceGrouper {

    private ReferenceGrouper() {
    }

    /** key -> sessions in ascending start order. */
    public static Map<String, List<Aggregate>> group(List<Event> events, long gap) {
        Map<String, List<Event>> byKey = new LinkedHashMap<>();
        for (Event e : events) {
            byKey.computeIfAbsent(e.key(), k -> new ArrayList<>()).add(e);
        }
        Map<String, List<Aggregate>> result = new LinkedHashMap<>();
        for (Map.Entry<String, List<Event>> entry : byKey.entrySet()) {
            List<Event> sorted = new ArrayList<>(entry.getValue());
            sorted.sort(Comparator.comparingLong(Event::timestamp).thenComparingDouble(Event::value));

            List<Aggregate> sessions = new ArrayList<>();
            Aggregate current = null;
            long lastTs = 0;
            for (Event e : sorted) {
                if (current == null || e.timestamp() - lastTs > gap) {
                    if (current != null) {
                        sessions.add(current);
                    }
                    current = Aggregate.single(e.timestamp(), e.value(), gap);
                } else {
                    current = current.merge(Aggregate.single(e.timestamp(), e.value(), gap));
                }
                lastTs = e.timestamp();
            }
            if (current != null) {
                sessions.add(current);
            }
            result.put(entry.getKey(), sessions);
        }
        return result;
    }
}
