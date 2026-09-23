package com.example.window;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Set;
import java.util.TreeMap;

/**
 * Event-time tumbling-window counting engine.
 *
 * <p>Semantics (all timestamps are abstract long values):
 * <ul>
 *   <li>Windows are left-closed, right-open: {@code [start, end)} with
 *       {@code start = floor(eventTime / windowSize) * windowSize}.</li>
 *   <li>Each partition carries an explicit, monotonically non-decreasing watermark.
 *       The global watermark is the minimum over the <b>active</b> partitions and
 *       never decreases.</li>
 *   <li>A window fires (emits a result) when {@code globalWatermark >= windowEnd}
 *       and its count changed since the last emission. Late events arriving within
 *       the allowed lateness therefore trigger a re-fire with the updated count.</li>
 *   <li>A window is purged when {@code globalWatermark >= windowEnd + allowedLateness};
 *       its latest emission is flagged {@code "final": true}. Events belonging to a
 *       purged window are written to the side output.</li>
 *   <li>Event ids are de-duplicated within a live window; duplicates are dropped
 *       and counted.</li>
 *   <li>A partition can be marked idle, which excludes it from the global-watermark
 *       minimum. Any event or watermark from that partition revives it.</li>
 * </ul>
 *
 * <p>All public methods are synchronized; the server also runs single-threaded, so
 * a given input sequence always produces the same output sequence.
 */
public final class WindowEngine {

    private final long windowSize;
    private final long allowedLateness;

    private long globalWatermark = Long.MIN_VALUE;
    private final Map<String, PartitionState> partitions = new LinkedHashMap<>();
    private final NavigableMap<Long, WindowState> windows = new TreeMap<>(); // key = windowStart
    private final List<Map<String, Object>> results = new ArrayList<>();
    private final List<Map<String, Object>> sideOutput = new ArrayList<>();
    private final List<Map<String, Object>> purges = new ArrayList<>();
    private long accepted;
    private long duplicates;

    public WindowEngine(long windowSize, long allowedLateness) {
        if (windowSize <= 0) {
            throw new IllegalArgumentException("windowSize must be > 0");
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness must be >= 0");
        }
        this.windowSize = windowSize;
        this.allowedLateness = allowedLateness;
    }

    private static final class PartitionState {
        private long watermark = Long.MIN_VALUE;
        private boolean idle;
    }

    private static final class WindowState {
        private final long start;
        private long count;
        private long emittedCount;
        private final Map<String, Long> byKey = new LinkedHashMap<>();
        private final Set<String> seenIds = new HashSet<>();

        private WindowState(long start) {
            this.start = start;
        }
    }

    // ---------- inputs ----------

    /**
     * Ingests one event. Returns a per-event status map:
     * ACCEPTED, DUPLICATE (id already seen in this window) or
     * LATE_SIDE_OUTPUT (the event's window has already been purged).
     */
    public synchronized Map<String, Object> addEvent(String id, String key, long eventTime, String partition) {
        PartitionState p = partitions.computeIfAbsent(partition, k -> new PartitionState());
        if (p.idle) {
            p.idle = false; // activity revives an idle partition
            recomputeGlobalWatermark();
        }
        long ws = windowStart(eventTime);
        long end = ws + windowSize;

        Map<String, Object> r = new LinkedHashMap<>();
        r.put("eventId", id);
        r.put("windowStart", ws);
        r.put("windowEnd", end);

        if (globalWatermark >= end + allowedLateness) {
            // The window is already purged: too late to count, route to side output.
            Map<String, Object> rec = new LinkedHashMap<>();
            rec.put("eventId", id);
            rec.put("key", key);
            rec.put("eventTime", eventTime);
            rec.put("partition", partition);
            rec.put("windowStart", ws);
            rec.put("windowEnd", end);
            rec.put("reason", "window already purged: globalWatermark=" + globalWatermark
                    + " >= windowEnd+allowedLateness=" + (end + allowedLateness));
            sideOutput.add(rec);
            r.put("status", "LATE_SIDE_OUTPUT");
        } else {
            WindowState w = windows.computeIfAbsent(ws, WindowState::new);
            if (!w.seenIds.add(id)) {
                duplicates++;
                r.put("status", "DUPLICATE");
            } else {
                w.count++;
                w.byKey.merge(key, 1L, Long::sum);
                accepted++;
                r.put("status", "ACCEPTED");
            }
        }
        r.put("globalWatermark", nullable(globalWatermark));
        return r;
    }

    /**
     * Advances a partition's watermark. Revives the partition if it was idle.
     *
     * @throws IllegalArgumentException on watermark regression
     */
    public synchronized Map<String, Object> submitWatermark(String partition, long watermark) {
        PartitionState p = partitions.computeIfAbsent(partition, k -> new PartitionState());
        p.idle = false; // a watermark is activity: revives an idle partition
        if (watermark < p.watermark) {
            throw new IllegalArgumentException("watermark regression on partition '" + partition
                    + "': " + watermark + " < current " + p.watermark);
        }
        p.watermark = watermark;
        recomputeGlobalWatermark();

        Map<String, Object> r = new LinkedHashMap<>();
        r.put("partition", partition);
        r.put("partitionWatermark", watermark);
        r.put("globalWatermark", nullable(globalWatermark));
        return r;
    }

    /** Marks a partition idle, excluding it from the global-watermark minimum. */
    public synchronized Map<String, Object> markIdle(String partition) {
        PartitionState p = partitions.computeIfAbsent(partition, k -> new PartitionState());
        p.idle = true;
        recomputeGlobalWatermark();

        Map<String, Object> r = new LinkedHashMap<>();
        r.put("partition", partition);
        r.put("idle", true);
        r.put("globalWatermark", nullable(globalWatermark));
        return r;
    }

    /** Clears all state (events, watermarks, windows, outputs). */
    public synchronized void reset() {
        globalWatermark = Long.MIN_VALUE;
        partitions.clear();
        windows.clear();
        results.clear();
        sideOutput.clear();
        purges.clear();
        accepted = 0;
        duplicates = 0;
    }

    // ---------- watermark machinery ----------

    private void recomputeGlobalWatermark() {
        long min = Long.MAX_VALUE;
        boolean anyActive = false;
        for (PartitionState p : partitions.values()) {
            if (p.idle) {
                continue;
            }
            anyActive = true;
            if (p.watermark < min) {
                min = p.watermark;
            }
        }
        long candidate = anyActive ? min : Long.MIN_VALUE;
        if (candidate > globalWatermark) {
            globalWatermark = candidate;
            onWatermarkAdvanced();
        }
    }

    private void onWatermarkAdvanced() {
        // 1) fire / re-fire windows that are complete at this watermark
        for (WindowState w : windows.values()) {
            long end = w.start + windowSize;
            if (end <= globalWatermark && w.count != w.emittedCount) {
                emit(w);
            }
        }
        // 2) purge windows whose allowed lateness has fully expired
        Iterator<Map.Entry<Long, WindowState>> it = windows.entrySet().iterator();
        while (it.hasNext()) {
            WindowState w = it.next().getValue();
            long end = w.start + windowSize;
            if (globalWatermark >= end + allowedLateness) {
                if (w.count != w.emittedCount) {
                    emit(w); // defensive: normally already emitted in step 1
                }
                markFinal(w.start);
                w.seenIds.clear();

                Map<String, Object> p = new LinkedHashMap<>();
                p.put("windowStart", w.start);
                p.put("windowEnd", end);
                p.put("purgedAtWatermark", globalWatermark);
                p.put("finalCount", w.count);
                purges.add(p);
                it.remove();
            }
        }
    }

    private void emit(WindowState w) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowStart", w.start);
        r.put("windowEnd", w.start + windowSize);
        r.put("count", w.count);
        r.put("byKey", new LinkedHashMap<>(w.byKey));
        r.put("closedAtWatermark", globalWatermark);
        r.put("final", false);
        results.add(r);
        w.emittedCount = w.count;
    }

    /** Flags the most recent emission of the given window as final. */
    private void markFinal(long windowStart) {
        for (int i = results.size() - 1; i >= 0; i--) {
            Map<String, Object> r = results.get(i);
            if ((Long) r.get("windowStart") == windowStart) {
                r.put("final", true);
                return;
            }
        }
    }

    private long windowStart(long eventTime) {
        return Math.floorDiv(eventTime, windowSize) * windowSize;
    }

    private static Long nullable(long watermark) {
        return watermark == Long.MIN_VALUE ? null : watermark;
    }

    // ---------- read views ----------

    public synchronized long globalWatermark() {
        return globalWatermark;
    }

    public synchronized long windowSize() {
        return windowSize;
    }

    public synchronized long allowedLateness() {
        return allowedLateness;
    }

    public synchronized long acceptedCount() {
        return accepted;
    }

    public synchronized long duplicateCount() {
        return duplicates;
    }

    public synchronized List<Map<String, Object>> results() {
        return new ArrayList<>(results);
    }

    public synchronized List<Map<String, Object>> sideOutput() {
        return new ArrayList<>(sideOutput);
    }

    public synchronized List<Map<String, Object>> purges() {
        return new ArrayList<>(purges);
    }

    /** Full state snapshot for the /api/state endpoint. */
    public synchronized Map<String, Object> snapshot() {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("windowSize", windowSize);
        s.put("allowedLateness", allowedLateness);
        s.put("globalWatermark", nullable(globalWatermark));

        Map<String, Object> ps = new LinkedHashMap<>();
        for (Map.Entry<String, PartitionState> e : partitions.entrySet()) {
            Map<String, Object> p = new LinkedHashMap<>();
            p.put("watermark", nullable(e.getValue().watermark));
            p.put("idle", e.getValue().idle);
            ps.put(e.getKey(), p);
        }
        s.put("partitions", ps);

        List<Object> open = new ArrayList<>();
        for (WindowState w : windows.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("windowStart", w.start);
            m.put("windowEnd", w.start + windowSize);
            m.put("count", w.count);
            m.put("byKey", new LinkedHashMap<>(w.byKey));
            open.add(m);
        }
        s.put("openWindows", open);
        s.put("results", new ArrayList<>(results));
        s.put("sideOutput", new ArrayList<>(sideOutput));
        s.put("purges", new ArrayList<>(purges));

        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("accepted", accepted);
        stats.put("duplicates", duplicates);
        stats.put("lateSideOutput", (long) sideOutput.size());
        s.put("stats", stats);
        return s;
    }
}
