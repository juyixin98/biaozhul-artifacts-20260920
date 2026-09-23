package sessionwindow;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.stream.Collectors;

/**
 * Core, network-free session-window aggregation logic.
 *
 * Semantics
 * ---------
 * Events are grouped by {@code key}. Within one key, events sorted by
 * (timestamp, arrival) form sessions: two adjacent events belong to the same
 * session when the gap between their timestamps is {@code <= gapMillis}.
 *
 * A late event can therefore bridge two sessions that had already been emitted
 * (the acceptance scenario: events arrive at t=0, t=20, then t=10 with gap=10).
 * Every ingest recomputes the affected key; the diff between the old and new
 * materialized result is emitted as a changelog:
 *   - a disappeared session emits RETRACT (old version),
 *   - a changed/continued session emits RETRACT then UPSERT of the new version,
 *   - a brand-new session emits UPSERT.
 *
 * Watermark: one global watermark = maxObservedEventTime - allowedLateness.
 * An event with timestamp strictly smaller than the watermark is rejected and
 * never participates in aggregation. Events at exactly the watermark are kept
 * (late but within the allowed lateness budget).
 *
 * The materialized view holds only the latest version of each session, so a
 * RETRACT followed by an UPSERT can never double-count events.
 *
 * This class is thread-safe: all public mutators synchronize on the instance.
 */
final class SessionAggregator {

    /** One input event. */
    record Event(String eventId, String key, long timestamp, long arrivalSeq) {
    }

    /** One emitted changelog message. */
    record Change(
            long seq,
            String type,            // "UPSERT" or "RETRACT"
            String sessionId,
            String key,
            long version,
            long startTs,
            long endTs,
            List<String> eventIds,
            Long eventTimestamp,
            String reason) {

        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("seq", seq);
            m.put("type", type);
            m.put("sessionId", sessionId);
            m.put("key", key);
            m.put("version", version);
            m.put("startTs", startTs);
            m.put("endTs", endTs);
            m.put("eventIds", eventIds == null ? List.of() : eventIds);
            m.put("eventTimestamp", eventTimestamp);
            m.put("reason", reason);
            return m;
        }
    }

    /** An aggregated session. Identity is sessionId; version increments per change. */
    static final class Session {
        final String sessionId;
        final String key;
        final long startTs;
        final long endTs;
        final List<String> eventIds;
        final long version;

        Session(String sessionId, String key, long startTs, long endTs,
                List<String> eventIds, long version) {
            this.sessionId = sessionId;
            this.key = key;
            this.startTs = startTs;
            this.endTs = endTs;
            this.eventIds = List.copyOf(eventIds);
            this.version = version;
        }

        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("sessionId", sessionId);
            m.put("key", key);
            m.put("startTs", startTs);
            m.put("endTs", endTs);
            m.put("durationMs", endTs - startTs);
            m.put("eventCount", eventIds.size());
            m.put("eventIds", eventIds);
            m.put("version", version);
            return m;
        }
    }

    // ---- configuration ----
    private long gapMillis;
    private final long allowedLatenessMillis;

    // ---- state ----
    /** key -> eventId -> event (dedupe by eventId within a key) */
    private final Map<String, LinkedHashMap<String, Event>> eventsByKey = new TreeMap<>();
    /** key -> sessionId -> current materialized session */
    private final Map<String, Map<String, Session>> materialized = new TreeMap<>();
    /** key -> sessionId -> current version (survives merges/deletes until reset) */
    private final Map<String, Map<String, Long>> versions = new TreeMap<>();

    private final List<Change> changelog = new ArrayList<>();
    private final List<Map<String, Object>> rejected = new ArrayList<>();

    private long maxObservedTs = Long.MIN_VALUE;
    private long watermark = Long.MIN_VALUE;
    private long arrivalCounter = 0;
    private long changeSeq = 0;
    private long acceptedCount = 0;
    private long rejectedCount = 0;
    /** Total event rows currently present in the materialized sessions (distinct events). */
    private long materializedEventRows = 0;

    SessionAggregator(long gapMillis, long allowedLatenessMillis) {
        if (gapMillis < 0 || allowedLatenessMillis < 0) {
            throw new IllegalArgumentException("gap and allowedLateness must be >= 0");
        }
        this.gapMillis = gapMillis;
        this.allowedLatenessMillis = allowedLatenessMillis;
    }

    long getGapMillis() {
        return gapMillis;
    }

    long getAllowedLatenessMillis() {
        return allowedLatenessMillis;
    }

    long getWatermark() {
        return watermark;
    }

    long getMaxObservedTs() {
        return maxObservedTs == Long.MIN_VALUE ? 0 : maxObservedTs;
    }

    long getAcceptedCount() {
        return acceptedCount;
    }

    long getRejectedCount() {
        return rejectedCount;
    }

    long getMaterializedEventRows() {
        return materializedEventRows;
    }

    List<Change> getChangelog() {
        synchronized (this) {
            return List.copyOf(changelog);
        }
    }

    List<Map<String, Object>> getRejected() {
        synchronized (this) {
            return List.copyOf(rejected);
        }
    }

    /** All currently materialized sessions, ordered by (key, startTs). */
    List<Session> getSessions() {
        synchronized (this) {
            List<Session> all = new ArrayList<>();
            for (Map<String, Session> byId : materialized.values()) {
                byId.values().stream()
                        .sorted((a, b) -> a.startTs == b.startTs
                                ? a.sessionId.compareTo(b.sessionId)
                                : Long.compare(a.startTs, b.startTs))
                        .forEach(all::add);
            }
            return all;
        }
    }

    /**
     * Accept one event and advance the state.
     *
     * @return changelog entries produced by this ingest (possibly empty on
     *         duplicate), or an empty optional-style list when rejected —
     *         callers must first check {@link IngestResult#accepted()}.
     */
    /** Thrown when an eventId is reused with a different timestamp. */
    static final class DuplicateIdException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        DuplicateIdException(String message) {
            super(message);
        }
    }

    IngestResult ingest(String eventId, String key, long timestamp) {
        synchronized (this) {
            arrivalCounter++;

            // Dedupe is checked before the watermark so an at-least-once retry
            // of an already-accepted event stays idempotent even after the
            // watermark has advanced past it.
            LinkedHashMap<String, Event> store =
                    eventsByKey.computeIfAbsent(key, k -> new LinkedHashMap<>());
            Event previous = store.get(eventId);
            if (previous != null) {
                if (previous.timestamp() == timestamp) {
                    return new IngestResult(true, true, eventId, key, timestamp,
                            watermark, List.of());
                }
                throw new DuplicateIdException("eventId '" + eventId
                        + "' already exists with timestamp " + previous.timestamp()
                        + "; cannot reinsert with timestamp " + timestamp);
            }

            if (timestamp < watermark) {
                rejectedCount++;
                Map<String, Object> rec = new LinkedHashMap<>();
                rec.put("eventId", eventId);
                rec.put("key", key);
                rec.put("timestamp", timestamp);
                rec.put("watermark", watermark);
                rec.put("reason", "timestamp " + timestamp + " < watermark " + watermark);
                rec.put("arrivalSeq", arrivalCounter);
                rejected.add(rec);
                return new IngestResult(false, false, eventId, key, timestamp, watermark, List.of());
            }

            Event event = new Event(eventId, key, timestamp, arrivalCounter);
            store.put(eventId, event);
            acceptedCount++;
            if (timestamp > maxObservedTs) {
                maxObservedTs = timestamp;
                watermark = maxObservedTs - allowedLatenessMillis;
            }
            List<Change> changes = recomputeKey(key, event);
            recomputeMaterializedRows();
            return new IngestResult(true, false, eventId, key, timestamp, watermark, changes);
        }
    }

    record IngestResult(boolean accepted, boolean duplicate, String eventId, String key,
                        long timestamp, long watermark, List<Change> changes) {
    }

    // ------------------------------------------------------------------
    // Pure sessionization
    // ------------------------------------------------------------------

    /**
     * Split one key's accepted events into sessions. Package-private and static
     * so unit tests can exercise it directly. Events are sorted by
     * (timestamp, arrivalSeq) to make ties deterministic.
     */
    static List<List<Event>> splitIntoSessions(Collection<Event> events, long gapMillis) {
        List<Event> sorted = events.stream()
                .sorted((a, b) -> a.timestamp() == b.timestamp()
                        ? Long.compare(a.arrivalSeq(), b.arrivalSeq())
                        : Long.compare(a.timestamp(), b.timestamp()))
                .collect(Collectors.toList());
        List<List<Event>> result = new ArrayList<>();
        List<Event> current = new ArrayList<>();
        for (Event e : sorted) {
            if (current.isEmpty()) {
                current.add(e);
                continue;
            }
            Event last = current.get(current.size() - 1);
            if (e.timestamp() - last.timestamp() <= gapMillis) {
                current.add(e);
            } else {
                result.add(current);
                current = new ArrayList<>();
                current.add(e);
            }
        }
        if (!current.isEmpty()) {
            result.add(current);
        }
        return result;
    }

    /** Stable session id: key + start timestamp. Survives merges for the surviving session. */
    private static String sessionIdOf(String key, long startTs) {
        return key + "@" + startTs;
    }

    // ------------------------------------------------------------------
    // Diff -> changelog
    // ------------------------------------------------------------------

    private List<Change> recomputeKey(String key, Event trigger) {
        Map<String, Session> old = materialized.computeIfAbsent(key, k -> new LinkedHashMap<>());
        Map<String, Long> ver = versions.computeIfAbsent(key, k -> new LinkedHashMap<>());

        Map<String, Session> fresh = new LinkedHashMap<>();
        for (List<Event> group : splitIntoSessions(eventsByKey.get(key).values(), gapMillis)) {
            long start = group.get(0).timestamp();
            long end = group.get(group.size() - 1).timestamp();
            List<String> ids = group.stream().map(Event::eventId).collect(Collectors.toList());
            String id = sessionIdOf(key, start);
            // Version bumps only when the body is new or actually changed;
            // unchanged sessions keep their version and emit nothing.
            Session before = old.get(id);
            long v;
            if (before == null) {
                v = ver.merge(id, 1L, Long::sum); // seed: 1
            } else {
                List<String> beforeIds = before.eventIds;
                boolean changed = before.startTs != start || before.endTs != end
                        || !beforeIds.equals(ids);
                v = changed ? ver.merge(id, 1L, Long::sum) : before.version;
            }
            fresh.put(id, new Session(id, key, start, end, ids, v));
        }

        List<Change> produced = new ArrayList<>();

        // 1) Sessions that disappeared (bridged away): RETRACT final version.
        for (Session gone : old.values()) {
            if (!fresh.containsKey(gone.sessionId)) {
                produced.add(emit("RETRACT", gone, trigger, "bridged into a later session"));
            }
        }
        // 2) Surviving sessions whose body changed: RETRACT old, UPSERT new.
        for (Session now : fresh.values()) {
            Session before = old.get(now.sessionId);
            if (before == null) {
                produced.add(emit("UPSERT", now, trigger, "new session"));
            } else if (!sameBody(before, now)) {
                produced.add(emit("RETRACT", before, trigger,
                        "superseded by version " + now.version));
                produced.add(emit("UPSERT", now, trigger,
                        "late event extended/merged the session"));
            }
            // Unchanged: no message (idempotent).
        }

        materialized.put(key, fresh);
        return produced;
    }

    private static boolean sameBody(Session a, Session b) {
        return a.startTs == b.startTs && a.endTs == b.endTs && a.eventIds.equals(b.eventIds);
    }

    private Change emit(String type, Session s, Event trigger, String reason) {
        changeSeq++;
        Change c = new Change(
                changeSeq,
                type,
                s.sessionId,
                s.key,
                s.version,
                s.startTs,
                s.endTs,
                s.eventIds,
                trigger == null ? null : trigger.timestamp(),
                reason);
        changelog.add(c);
        return c;
    }

    private void recomputeMaterializedRows() {
        long rows = 0;
        for (Map<String, Session> byId : materialized.values()) {
            for (Session s : byId.values()) {
                rows += s.eventIds.size();
            }
        }
        materializedEventRows = rows;
    }

    // ------------------------------------------------------------------
    // Snapshots (used by HTTP layer + tests)
    // ------------------------------------------------------------------

    Map<String, Object> stateMap() {
        synchronized (this) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("gapMillis", gapMillis);
            m.put("allowedLatenessMillis", allowedLatenessMillis);
            m.put("watermark", watermark == Long.MIN_VALUE ? null : watermark);
            m.put("maxObservedTs", maxObservedTs == Long.MIN_VALUE ? null : maxObservedTs);
            m.put("acceptedCount", acceptedCount);
            m.put("rejectedCount", rejectedCount);
            m.put("materializedSessionCount", getSessions().size());
            m.put("materializedEventRows", materializedEventRows);
            return m;
        }
    }

    Map<String, Object> accountingMap() {
        synchronized (this) {
            long distinctEvents = eventsByKey.values().stream()
                    .mapToLong(LinkedHashMap::size).sum();
            long retractCount = changelog.stream().filter(c -> c.type().equals("RETRACT")).count();
            long upsertCount = changelog.stream().filter(c -> c.type().equals("UPSERT")).count();

            Map<String, Object> m = new LinkedHashMap<>();
            m.put("acceptedEvents", acceptedCount);
            m.put("rejectedEvents", rejectedCount);
            m.put("distinctStoredEvents", distinctEvents);
            m.put("materializedSessions", getSessions().size());
            m.put("materializedEventRows", materializedEventRows);
            m.put("upsertMessages", upsertCount);
            m.put("retractMessages", retractCount);
            m.put("noDoubleCount", materializedEventRows == distinctEvents);
            m.put("watermark", watermark == Long.MIN_VALUE ? null : watermark);
            return m;
        }
    }

    /**
     * Replay the given events in order into a fresh aggregator. Used both by
     * persistence recovery and by the stability test.
     */
    static SessionAggregator replay(long gapMillis, long allowedLateness,
                                    List<Event> events) {
        SessionAggregator agg = new SessionAggregator(gapMillis, allowedLateness);
        for (Event e : events) {
            agg.ingest(e.eventId(), e.key(), e.timestamp());
        }
        return agg;
    }

    /** Accepted events in arrival order (for persistence). */
    List<Event> acceptedEventsInArrivalOrder() {
        synchronized (this) {
            return eventsByKey.values().stream()
                    .flatMap(m -> m.values().stream())
                    .filter(Objects::nonNull)
                    .sorted((a, b) -> Long.compare(a.arrivalSeq(), b.arrivalSeq()))
                    .collect(Collectors.toList());
        }
    }
}
