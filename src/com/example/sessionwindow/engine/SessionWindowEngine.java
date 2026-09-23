package com.example.sessionwindow.engine;

import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Event-time session window engine.
 *
 * <h2>Window rule</h2>
 * <p>Per key, events are grouped into sessions using an inactivity
 * {@code gap} G: two consecutive (time-ordered) events belong to the same
 * session iff their distance is {@code <= G}. Equivalently, an event at time t
 * joins a session with span [start, lastTs + G] iff
 * {@code start - G < t <= lastTs + G}. The interval is half-open: an event at
 * exactly {@code start - G} opens a separate session (the "interval equal to
 * gap" boundary case).</p>
 *
 * <h2>Watermarks and allowed lateness</h2>
 * <ul>
 *   <li>A session is <b>sealed</b> when {@code watermark >= sessionEnd}
 *       (sessionEnd = lastTimestamp + gap). A SEALED record is emitted; the
 *       result is final <i>unless</i> a late event reopens it.</li>
 *   <li>An event with {@code timestamp + allowedLateness < watermark} is too
 *       late: it is dropped (DROPPED record) and touches no state.</li>
 *   <li>A late event within allowedLateness may join one sealed session or
 *       bridge two sessions; every superseded result produces an explicit
 *       RETRACT and the merged session emits a fresh ADD (and SEALED again when
 *       its new end passes the watermark).</li>
 *   <li>A sealed session's state is <b>purged</b> once
 *       {@code watermark >= sessionEnd + allowedLateness} (PURGED record). After
 *       purging it cannot be reopened: any such event is dropped.</li>
 * </ul>
 *
 * <h2>Assumptions</h2>
 * Timestamps, gap and allowedLateness must be bounded so that
 * {@code timestamp + gap} and {@code timestamp + allowedLateness} do not
 * overflow {@code long}.
 */
public class SessionWindowEngine {

    private final long gap;
    private final long allowedLateness;
    private long watermark = Long.MIN_VALUE;

    /** key -> sessions sorted ascending by start; stored in insertion order of keys. */
    private final Map<String, List<Session>> sessionsByKey = new LinkedHashMap<>();
    private final List<ResultRecord> records = new ArrayList<>();

    public SessionWindowEngine(long gap, long allowedLateness) {
        if (gap <= 0) {
            throw new IllegalArgumentException("gap must be positive, got " + gap);
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness must be >= 0, got " + allowedLateness);
        }
        this.gap = gap;
        this.allowedLateness = allowedLateness;
    }

    public long gap() {
        return gap;
    }

    public long allowedLateness() {
        return allowedLateness;
    }

    public long watermark() {
        return watermark;
    }

    /** All records emitted since construction / last {@link #drainRecords()}. */
    public List<ResultRecord> records() {
        return records;
    }

    public List<ResultRecord> drainRecords() {
        List<ResultRecord> out = new ArrayList<>(records);
        records.clear();
        return out;
    }

    // ------------------------------------------------------------------
    // Input
    // ------------------------------------------------------------------

    public void processEvent(Event event) {
        if (event == null || event.key() == null) {
            throw new IllegalArgumentException("event and event.key() must be non-null");
        }
        long ts = event.timestamp();

        // Too-late events are ignored before touching any state.
        // Boundary: timestamp == watermark - allowedLateness is still accepted.
        // Written as a subtraction to avoid ts + allowedLateness overflow.
        if (watermark != Long.MIN_VALUE && ts < watermark - allowedLateness) {
            records.add(ResultRecord.dropped(event, watermark));
            return;
        }

        List<Session> sessions = sessionsByKey.computeIfAbsent(event.key(), k -> new ArrayList<>());
        List<Session> overlaps = new ArrayList<>(2);
        for (Session s : sessions) {
            // An event may overlap (touch) two sessions at once: keep scanning
            // so a boundary event bridges both.
            if (s.overlaps(ts, gap)) {
                overlaps.add(s);
            }
        }

        if (overlaps.isEmpty()) {
            Session created = new Session(ts, event.value(), gap);
            insertInOrder(sessions, created);
            publishAdd(event.key(), created);
            reconcile(event.key(), created, sessions);
            return;
        }

        // Snapshot superseded results before mutating anything.
        List<Aggregate> toRetract = new ArrayList<>();
        boolean anySealed = false;
        for (Session s : overlaps) {
            if (s.published()) {
                toRetract.add(s.aggregate());
            }
            anySealed |= s.sealed();
        }

        Session anchor = overlaps.get(0);
        anchor.addEvent(ts, event.value(), gap);
        for (int i = 1; i < overlaps.size(); i++) {
            anchor.mergeFrom(overlaps.get(i));
        }
        sessions.removeAll(overlaps.subList(1, overlaps.size()));
        if (anySealed) {
            // Reopening a sealed session: revoke sealedness so reconcile can
            // re-emit SEALED after the merged aggregate stabilises.
            anchor.markReopened();
        }

        for (Aggregate old : toRetract) {
            records.add(ResultRecord.retract(event.key(), old));
        }
        publishAdd(event.key(), anchor);
        // start may have shrunk through a merged earlier session: keep sorted order.
        sessions.sort((a, b) -> Long.compare(a.start(), b.start()));
        reconcile(event.key(), anchor, sessions);
    }

    public void processWatermark(long newWatermark) {
        if (newWatermark <= watermark) {
            return; // watermarks are monotonic
        }
        watermark = newWatermark;
        records.add(ResultRecord.watermark(newWatermark));

        // Snapshot keys: sealAndPurge may delete emptied key entries.
        for (String key : new ArrayList<>(sessionsByKey.keySet())) {
            List<Session> sessions = sessionsByKey.get(key);
            if (sessions != null) {
                sealAndPurge(key, sessions);
            }
        }
    }

    /**
     * Advance the watermark to +infinity: every session is sealed and purged,
     * leaving no state behind. Returns nothing; records land in the log.
     */
    public void flush() {
        processWatermark(Long.MAX_VALUE);
    }

    // ------------------------------------------------------------------
    // State inspection (used by tests and the service)
    // ------------------------------------------------------------------

    public record SessionInfo(String key, Aggregate aggregate, boolean sealed) {
    }

    /** Current retained sessions across all keys, in key/start order. */
    public List<SessionInfo> snapshot() {
        List<SessionInfo> out = new ArrayList<>();
        for (Map.Entry<String, List<Session>> e : sessionsByKey.entrySet()) {
            for (Session s : e.getValue()) {
                out.add(new SessionInfo(e.getKey(), s.aggregate(), s.sealed()));
            }
        }
        return out;
    }

    public int sessionCount(String key) {
        List<Session> sessions = sessionsByKey.get(key);
        return sessions == null ? 0 : sessions.size();
    }

    public int totalSessionCount() {
        int total = 0;
        for (List<Session> sessions : sessionsByKey.values()) {
            total += sessions.size();
        }
        return total;
    }

    public int keyCount() {
        return sessionsByKey.size();
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    private void publishAdd(String key, Session s) {
        s.markPublished();
        records.add(ResultRecord.add(key, s.aggregate()));
    }

    /**
     * After an insertion/merge the anchor may already be at or behind the
     * watermark (a late event within allowedLateness): seal it and, if
     * allowedLateness is exhausted, purge it immediately.
     */
    private void reconcile(String key, Session anchor, List<Session> sessions) {
        if (!anchor.sealed() && anchor.end() <= watermark) {
            anchor.markSealed();
            records.add(ResultRecord.sealed(key, anchor.aggregate()));
        }
        if (anchor.sealed() && watermark - anchor.end() >= allowedLateness) {
            sessions.remove(anchor);
            if (sessions.isEmpty()) {
                sessionsByKey.remove(key);
            }
            records.add(ResultRecord.purged(key, anchor.aggregate()));
        }
    }

    private void sealAndPurge(String key, List<Session> sessions) {
        if (sessions.isEmpty()) {
            sessionsByKey.remove(key);
            return;
        }
        List<Session> purged = new ArrayList<>();
        for (Session s : sessions) {
            if (!s.sealed() && s.end() <= watermark) {
                s.markSealed();
                records.add(ResultRecord.sealed(key, s.aggregate()));
            }
            if (s.sealed() && watermark - s.end() >= allowedLateness) {
                purged.add(s);
            }
        }
        if (!purged.isEmpty()) {
            sessions.removeAll(purged);
            for (Session s : purged) {
                records.add(ResultRecord.purged(key, s.aggregate()));
            }
            if (sessions.isEmpty()) {
                sessionsByKey.remove(key);
            }
        }
    }

    private static void insertInOrder(List<Session> sessions, Session created) {
        int pos = 0;
        while (pos < sessions.size() && sessions.get(pos).start() < created.start()) {
            pos++;
        }
        sessions.add(pos, created);
    }
}
