package com.example.sessionwindow;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Dynamic session-merging engine.
 *
 * Events for the same key are aggregated into sessions; two consecutive events
 * belong to the same session when their distance is <= gap. Sessions never split:
 * a late event can land between two existing sessions within gap of both and
 * bridge (merge) them. Every materialized update is emitted as a versioned
 * changelog record (UPSERT / RETRACT); consumers fold it into a materialized view.
 *
 * Watermark rule: watermark W = max(manual floor, maxEventTs - allowedLateness).
 * An event with ts < W arriving AFTER W was reached is rejected (not counted,
 * not logged, does not affect output). An event exactly AT W is still accepted
 * (boundary convention: gap and watermark are inclusive of equal timestamps,
 * which is what lets the 0/20 + late-10 scenario bridge at gap=lateness=10).
 * Idempotent re-delivery with the same clientId is answered as DUPLICATE and
 * never counted twice.
 *
 * Session ids are stable: key-local monotonic integers; a merged session keeps
 * the smaller id. Replaying the append-only event log in order reconstructs
 * state, changelog and ids byte-for-byte.
 */
public final class SessionEngine {

    public enum Status {
        ACCEPTED, REJECTED, DUPLICATE
    }

    public record Result(Status status, long revision, Long watermark, List<Change> changes, String reason) {
    }

    private final long gap;
    private final long allowedLateness;
    private final EventLog log;

    // key -> sessions ordered by startTs (ties broken by localId)
    private final Map<String, TreeSet<Session>> sessions = new HashMap<>();
    private final Map<String, Integer> maxLocalId = new HashMap<>();
    private final Set<String> seenClientIds = new HashSet<>();
    private final List<Change> changelog = new ArrayList<>();

    private long seq = 0;
    private long revision = 0;
    private Long maxEventTs = null;
    private Long manualWatermark = null;
    private boolean replaying = false;

    // statistics (transient; rebuilt on replay as events are re-applied)
    private long received = 0;
    private long accepted = 0;
    private long rejected = 0;
    private long duplicates = 0;

    public SessionEngine(long gap, long allowedLateness, EventLog log) {
        if (gap < 0) {
            throw new IllegalArgumentException("gap must be >= 0");
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness must be >= 0");
        }
        this.gap = gap;
        this.allowedLateness = allowedLateness;
        this.log = log;
        if (log != null) {
            replay();
        }
    }

    public long gap() {
        return gap;
    }

    public long allowedLateness() {
        return allowedLateness;
    }

    // ---------------- watermark ----------------

    /** Effective watermark, or null when it has never been defined. */
    public synchronized Long watermark() {
        Long auto = maxEventTs == null ? null : maxEventTs - allowedLateness;
        if (manualWatermark == null) {
            return auto;
        }
        return auto == null ? manualWatermark : Math.max(manualWatermark, auto);
    }

    /**
     * Manually advance the watermark floor; never goes backwards.
     * A no-op advance (requested <= current floor) does not consume a revision.
     */
    public synchronized long advanceWatermark(long requested) {
        if (manualWatermark != null && requested <= manualWatermark) {
            return revision; // already at or beyond the requested floor
        }
        revision++;
        manualWatermark = requested;
        if (!replaying && log != null) {
            log.append(Map.of("type", "watermark", "wm", requested));
        }
        return revision;
    }

    // ---------------- event ingestion ----------------

    public Result submit(Event event) {
        synchronized (this) {
            return processAtCurrentRevision(event);
        }
    }

    private Result processAtCurrentRevision(Event event) {
        received++;

        // 1) idempotency (not a durable decision: no revision, no log record)
        if (event.clientId() != null && seenClientIds.contains(event.clientId())) {
            duplicates++;
            return new Result(Status.DUPLICATE, revision, watermark(), List.of(),
                    "clientId already processed");
        }

        // 2) watermark rejection (strictly behind the watermark; ts == wm accepted).
        //    Not durable: no revision, no log record, so it cannot affect recovery.
        Long wm = watermark();
        if (wm != null && event.ts() < wm) {
            rejected++;
            return new Result(Status.REJECTED, revision, wm, List.of(),
                    "event ts " + event.ts() + " < watermark " + wm);
        }

        // 3) a durable decision: allocate a revision and persist first so an
        //    accepted event survives a crash. During replay it is already logged.
        revision++;
        long rev = revision;
        if (!replaying && log != null) {
            log.append(event.toLogMap());
        }
        if (event.clientId() != null) {
            seenClientIds.add(event.clientId());
        }
        accepted++;
        if (maxEventTs == null || event.ts() > maxEventTs) {
            maxEventTs = event.ts();
        }

        // 4) find every session this event touches:
        //    start-gap <= ts <= end+gap
        TreeSet<Session> set = sessions.computeIfAbsent(event.key(),
                k -> new TreeSet<>(Comparator.comparingLong(Session::startTs)
                        .thenComparingInt(Session::localId)));

        List<Session> touched = new ArrayList<>(2);
        for (Session s : set) {
            if (event.ts() >= s.startTs() - gap && event.ts() <= s.endTs() + gap) {
                touched.add(s);
            }
        }

        List<Change> changes = new ArrayList<>(2);

        if (touched.isEmpty()) {
            // brand-new session
            int id = maxLocalId.merge(event.key(), 1, Integer::sum);
            Session s = new Session(id, event.ts(), event.ts(), 1, 1);
            set.add(s);
            changes.add(emit(Change.Kind.UPSERT, event.key(), s));
        } else if (touched.size() == 1) {
            // extend / prepend to an existing session
            Session s = touched.get(0);
            set.remove(s);
            s.addEvent(event.ts());
            set.add(s);
            changes.add(emit(Change.Kind.UPSERT, event.key(), s));
        } else {
            // a late event bridging two (or more) sessions:
            // survivor = smallest localId; retract all others, upsert merged.
            touched.sort(Comparator.comparingInt(Session::localId));
            Session survivor = touched.get(0);
            set.remove(survivor);
            for (int i = 1; i < touched.size(); i++) {
                Session other = touched.get(i);
                set.remove(other);
                // retract carries the last published version of the dead session
                changes.add(emit(Change.Kind.RETRACT, event.key(), other));
                survivor.merge(other, 0);
            }
            survivor.addEvent(event.ts()); // the bridging event itself
            set.add(survivor);
            changes.add(emit(Change.Kind.UPSERT, event.key(), survivor));
        }

        return new Result(Status.ACCEPTED, rev, watermark(), List.copyOf(changes), null);
    }

    private Change emit(Change.Kind kind, String key, Session s) {
        seq++;
        Change c = new Change(seq, revision, kind, key + "#" + s.localId(), key, s.version(),
                s.startTs(), s.endTs(), s.count(), watermark());
        changelog.add(c);
        return c;
    }

    // ---------------- views ----------------

    public synchronized Map<String, List<Map<String, Object>>> sessions() {
        var out = new LinkedHashMap<String, List<Map<String, Object>>>();
        var keys = new ArrayList<>(sessions.keySet());
        java.util.Collections.sort(keys);
        for (String key : keys) {
            var list = new ArrayList<Map<String, Object>>();
            for (Session s : sessions.get(key)) {
                list.add(s.toMap(key));
            }
            out.put(key, list);
        }
        return out;
    }

    public synchronized List<Change> changelog() {
        return List.copyOf(changelog);
    }

    public synchronized List<Change> changesSince(long afterSeq) {
        var out = new ArrayList<Change>();
        for (Change c : changelog) {
            if (c.seq() > afterSeq) {
                out.add(c);
            }
        }
        return out;
    }

    public synchronized Map<String, Object> stats() {
        var m = new LinkedHashMap<String, Object>();
        m.put("received", received);
        m.put("accepted", accepted);
        m.put("rejected", rejected);
        m.put("duplicates", duplicates);
        m.put("lastSeq", seq);
        m.put("lastRevision", revision);
        m.put("watermark", watermark());
        m.put("gap", gap);
        m.put("allowedLateness", allowedLateness);
        int sessionCount = 0;
        for (TreeSet<Session> set : sessions.values()) {
            sessionCount += set.size();
        }
        m.put("activeSessions", sessionCount);
        return m;
    }

    // ---------------- recovery ----------------

    private void replay() {
        replaying = true;
        try {
            for (Map<String, Object> rec : log.replay()) {
                String type = String.valueOf(rec.get("type"));
                switch (type) {
                    case "event" -> {
                        Event e = Event.fromLogMap(rec);
                        // processAtCurrentRevision allocates the same revision it
                        // would have live, so revision/seq are identical after recovery
                        Result r = processAtCurrentRevision(e);
                        if (r.status() != Status.ACCEPTED) {
                            throw new IllegalStateException(
                                    "Replay nondeterminism: logged event was not re-accepted: "
                                            + e + " -> " + r.status());
                        }
                    }
                    case "watermark" -> {
                        long wm = ((Number) rec.get("wm")).longValue();
                        revision++;
                        manualWatermark = Math.max(
                                manualWatermark == null ? Long.MIN_VALUE : manualWatermark, wm);
                    }
                    default -> throw new IllegalStateException("Unknown log record type: " + type);
                }
            }
        } finally {
            replaying = false;
        }
    }

    /** Test/reset hook: clear all in-memory state and the on-disk log. */
    public synchronized void reset() {
        sessions.clear();
        maxLocalId.clear();
        seenClientIds.clear();
        changelog.clear();
        seq = 0;
        revision = 0;
        maxEventTs = null;
        manualWatermark = null;
        received = accepted = rejected = duplicates = 0;
        if (log != null) {
            log.reset();
        }
    }
}
