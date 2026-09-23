package io.example.orderedcommit;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * All state belonging to one partition.
 *
 * <p>A partition owns:
 * <ul>
 *   <li>the monotonically increasing input sequence number assigned to each
 *       submitted event;</li>
 *   <li>{@code pending}, all not-yet-committed events (bounded by the
 *       partition's buffer capacity, so an unresolvable head event exerts
 *       backpressure onto submitters);</li>
 *   <li>{@code committed}, the append-only output, ordered by sequence.</li>
 * </ul>
 */
final class Partition {

    final String name;
    final long bufferCap;

    /** Events in seq order that have no committed result yet. */
    final List<Event> pending = new ArrayList<>();
    /** Committed output in seq order; cancelled events do not appear here. */
    final List<Committed> committed = new ArrayList<>();
    /** Lookup of every event ever submitted to this partition, by id. */
    final Map<String, Event> eventsById = new HashMap<>();

    long nextSeq;
    long createdMillis;

    Partition(String name, long bufferCap, long createdMillis) {
        this.name = name;
        this.bufferCap = bufferCap;
        this.createdMillis = createdMillis;
    }

    Event assign(Object payload, long delay, boolean fail, long timeout, int attempts, long retryDelay) {
        Event event =
                new Event(
                        name,
                        name + "-" + nextSeq,
                        nextSeq,
                        payload,
                        delay,
                        fail,
                        timeout,
                        attempts,
                        retryDelay,
                        System.currentTimeMillis());
        nextSeq++;
        pending.add(event);
        return event;
    }

    /** Number of events occupying a buffer slot (queued, retry-waiting or running). */
    int outstanding() {
        int n = 0;
        for (Event e : pending) {
            if (!e.isTerminal()) {
                n++;
            }
        }
        return n;
    }

    Map<String, Object> stats() {
        int queued = 0;
        int running = 0;
        for (Event e : pending) {
            if (!e.isTerminal()) {
                queued++;
                if (e.inFlight) {
                    running++;
                }
            }
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("bufferCap", bufferCap);
        m.put("outstanding", queued);
        m.put("inFlight", running);
        m.put("buffered", queued - running);
        m.put("committedCount", committed.size());
        m.put("nextSeq", nextSeq);
        m.put("createdAt", createdMillis);
        return m;
    }
}
