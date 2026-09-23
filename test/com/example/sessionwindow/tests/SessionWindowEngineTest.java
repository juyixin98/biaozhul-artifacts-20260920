package com.example.sessionwindow.tests;

import com.example.sessionwindow.engine.Results;
import com.example.sessionwindow.engine.SessionWindowEngine;
import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertFalse;
import static com.example.sessionwindow.tests.Assert.assertTrue;

public class SessionWindowEngineTest {

    private static List<ResultRecord.Type> types(List<ResultRecord> records) {
        return records.stream().map(ResultRecord::type).toList();
    }

    private static ResultRecord firstOfType(List<ResultRecord> records, ResultRecord.Type type) {
        for (ResultRecord r : records) {
            if (r.type() == type) {
                return r;
            }
        }
        throw new AssertionError("no record of type " + type);
    }

    // ------------------------------------------------------------------
    // Basic session formation
    // ------------------------------------------------------------------

    @Test
    void inOrderEventsWithinGapFormOneSession() {
        SessionWindowEngine engine = new SessionWindowEngine(10, 0);
        engine.processEvent(Event.of("u1", 1, 2.0));
        engine.processEvent(Event.of("u1", 5, 3.0));
        engine.processEvent(Event.of("u1", 10, 5.0));
        assertEquals(1, engine.totalSessionCount(), "one session");
        Aggregate agg = engine.snapshot().get(0).aggregate();
        assertEquals(1L, agg.start(), "start");
        assertEquals(20L, agg.end(), "end = lastTs(10)+gap(10)");
        assertEquals(3L, agg.count(), "count");
        assertEquals(10.0, agg.sum(), 1e-9, "sum");
        assertEquals(2.0, agg.min(), 1e-9, "min");
        assertEquals(5.0, agg.max(), 1e-9, "max");
    }

    @Test
    void eventsFartherThanGapFormSeparateSessions() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 0);
        engine.processEvent(Event.of("u1", 1));
        engine.processEvent(Event.of("u1", 7)); // distance 6 > 5
        assertEquals(2, engine.totalSessionCount(), "two sessions");
    }

    @Test
    void keysArePartitionedIndependently() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 0);
        engine.processEvent(Event.of("a", 1));
        engine.processEvent(Event.of("b", 1));
        engine.processEvent(Event.of("a", 2));
        engine.processEvent(Event.of("b", 20));
        assertEquals(2, engine.keyCount(), "two keys");
        assertEquals(1, engine.sessionCount("a"), "a one session");
        assertEquals(2, engine.sessionCount("b"), "b two sessions");
    }

    // ------------------------------------------------------------------
    // Boundary: interval exactly equal to the gap
    // ------------------------------------------------------------------

    @Test
    void touchingAtExactlyGapMergesBothSessions() {
        // ACCEPTANCE boundary case: two anchors 20 apart (distance 20 > gap 10)
        // give two sealed sessions [0,10] and [20,30]. A late event at t=10 has
        // window [10,20], touching the first span at exactly 10 and the second
        // at exactly 20 -> it bridges BOTH. Offline, the sorted events
        // (0,10,20) have consecutive distances exactly equal to the gap, so
        // they are a single session.
        SessionWindowEngine engine = new SessionWindowEngine(10, 100);
        engine.processEvent(Event.of("u1", 0));  // [0,10]
        engine.processEvent(Event.of("u1", 20)); // [20,30]
        assertEquals(2, engine.totalSessionCount(), "anchors 20 apart split");
        engine.processWatermark(30);
        assertEquals(2, engine.totalSessionCount(), "sealed but retained (lateness 100)");

        engine.processEvent(Event.of("u1", 10)); // window [10,20] touches both
        assertEquals(1, engine.totalSessionCount(), "boundary event bridges both sessions");
        Aggregate merged = engine.snapshot().get(0).aggregate();
        assertEquals(0L, merged.start(), "merged start 0");
        assertEquals(30L, merged.end(), "merged end 30");
        assertEquals(3L, merged.count(), "three events");

        List<ResultRecord> records = engine.records();
        assertEquals(2L, records.stream().filter(r -> r.type() == ResultRecord.Type.RETRACT).count(),
                "both old windows retracted");
    }

    @Test
    void eventOneBeyondGapDoesNotBridge() {
        // Symmetric negative boundary: anchors 0 and 21 with gap 10. Event at 10
        // has window [10,20] which does NOT reach the later span start 21.
        SessionWindowEngine engine = new SessionWindowEngine(10, 100);
        engine.processEvent(Event.of("u1", 0));  // [0,10]
        engine.processEvent(Event.of("u1", 21)); // [21,31]
        engine.processWatermark(5);
        engine.processEvent(Event.of("u1", 10)); // joins first only
        assertEquals(2, engine.totalSessionCount(), "no bridge: 20 < 21");
    }

    @Test
    void eventExactlyAtSessionEndStillJoins() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 0);
        engine.processEvent(Event.of("u1", 1)); // end = 6
        engine.processEvent(Event.of("u1", 6)); // exactly at end
        assertEquals(1, engine.totalSessionCount(), "event exactly at end joins");
        assertEquals(11L, engine.snapshot().get(0).aggregate().end(), "new end = 6+5");
    }

    // ------------------------------------------------------------------
    // Watermark sealing
    // ------------------------------------------------------------------

    @Test
    void watermarkSealsSessionsAtEnd() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 5);
        engine.processEvent(Event.of("u1", 1)); // end = 1+5 = 6
        engine.processWatermark(5);
        assertFalse(firstSealed(engine), "wm 5 < end 6: not sealed");
        engine.processWatermark(6);
        assertTrue(firstSealed(engine), "wm 6 == end: sealed");
        assertEquals(1, engine.totalSessionCount(), "sealed but retained within lateness");
        List<ResultRecord.Type> types = types(engine.records());
        assertTrue(types.contains(ResultRecord.Type.SEALED), "SEALED emitted");
        assertFalse(types.contains(ResultRecord.Type.PURGED), "not purged yet (wm-end=0 < 5)");

        // Once watermark advances past end + allowedLateness it is purged.
        engine.processWatermark(11); // 11-6 >= 5
        assertTrue(types(engine.records()).contains(ResultRecord.Type.PURGED), "PURGED emitted");
        assertEquals(0, engine.totalSessionCount(), "state removed after lateness exhausted");
    }

    @Test
    void watermarkIsMonotonic() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 100);
        engine.processWatermark(10);
        engine.processWatermark(5); // ignored
        engine.processWatermark(9); // ignored
        assertEquals(10L, engine.watermark(), "watermark stays at max");
        long watermarkMarkers = engine.records().stream()
                .filter(r -> r.type() == ResultRecord.Type.WATERMARK).count();
        assertEquals(1L, watermarkMarkers, "one WATERMARK record");
    }

    // ------------------------------------------------------------------
    // ACCEPTANCE 1: out-of-order event bridges two sealed sessions
    // ------------------------------------------------------------------

    @Test
    void lateEventBridgesTwoSealedSessionsWithRetractAndAdd() {
        // Anchors 0 and 15 are 15 apart (> gap 10) -> two sessions [0,10]
        // and [15,25]. After both are SEALED by wm=25, a late event at t=10
        // (window [10,20]) overlaps both and bridges them.
        SessionWindowEngine engine = new SessionWindowEngine(10, 100);
        engine.processEvent(Event.of("u1", 0, 1.0));  // [0,10]
        engine.processEvent(Event.of("u1", 15, 4.0)); // [15,25]
        engine.processWatermark(25);
        assertTrue(engine.snapshot().stream().allMatch(SessionWindowEngine.SessionInfo::sealed),
                "two sealed sessions");
        assertEquals(2, engine.totalSessionCount(), "two sessions before bridge");

        engine.processEvent(Event.of("u1", 10, 9.0));

        assertEquals(1, engine.totalSessionCount(), "bridged into one session");
        Aggregate merged = engine.snapshot().get(0).aggregate();
        assertEquals(0L, merged.start(), "merged start");
        assertEquals(25L, merged.end(), "merged end kept (25 = max end)");
        assertEquals(3L, merged.count(), "three events");
        assertEquals(14.0, merged.sum(), 1e-9, "sum 1+4+9");

        List<ResultRecord> records = engine.records();
        long retracts = records.stream().filter(r -> r.type() == ResultRecord.Type.RETRACT).count();
        assertEquals(2L, retracts, "both sealed windows retracted");
        ResultRecord add = records.stream()
                .filter(r -> r.type() == ResultRecord.Type.ADD && r.aggregate().count() == 3)
                .findFirst().orElseThrow(() -> new AssertionError("merged ADD missing"));
        assertEquals(0L, add.aggregate().start(), "new add start");
        ResultRecord reSealed = records.stream()
                .filter(r -> r.type() == ResultRecord.Type.SEALED && r.aggregate().count() == 3)
                .findFirst().orElseThrow(() -> new AssertionError("re-SEALED missing"));
        assertEquals(25L, reSealed.aggregate().end(), "re-sealed merged end");
    }

    @Test
    void bridgedResultMatchesOfflineReference() {
        SessionWindowEngine engine = new SessionWindowEngine(10, 100);
        List<Event> all = new ArrayList<>();
        feed(engine, all, Event.of("u1", 0, 1.0));
        feed(engine, all, Event.of("u1", 15, 4.0));
        engine.processWatermark(25);
        feed(engine, all, Event.of("u1", 10, 9.0)); // late bridge
        engine.flush();

        Map<String, List<Aggregate>> online =
                com.example.sessionwindow.engine.Results.byKey(engine.records()).entrySet()
                        .stream().collect(java.util.stream.Collectors.toMap(
                                Map.Entry::getKey, e -> new ArrayList<>(e.getValue().values())));
        Map<String, List<Aggregate>> offline =
                com.example.sessionwindow.engine.ReferenceGrouper.group(all, 10);
        assertEquals(offline, online, "online folded results equal offline grouping");
        assertEquals(0, engine.totalSessionCount(), "all state purged after flush");
    }

    private static void feed(SessionWindowEngine engine, List<Event> sink, Event e) {
        engine.processEvent(e);
        sink.add(e);
    }

    // ------------------------------------------------------------------
    // ACCEPTANCE 3: late event after sealing, within vs beyond lateness
    // ------------------------------------------------------------------

    @Test
    void lateEventWithinLatenessReopensSealedSession() {
        SessionWindowEngine engine = new SessionWindowEngine(10, 10);
        engine.processEvent(Event.of("u1", 0)); // [0,10]
        engine.processWatermark(10); // sealed, not purged (wm-end=0 < 10)
        assertTrue(firstSealed(engine), "sealed");
        assertEquals(1, engine.totalSessionCount(), "retained for allowed lateness");

        engine.processEvent(Event.of("u1", 5, 7.0)); // late but wm-allowed: 5 >= 10-10
        assertEquals(1, engine.totalSessionCount(), "still one session");
        Aggregate agg = engine.snapshot().get(0).aggregate();
        assertEquals(2L, agg.count(), "late event counted");
        assertEquals(8.0, agg.sum(), 1e-9, "sum 1 + 7 updated");
        List<ResultRecord> records = engine.records();
        assertTrue(types(records).contains(ResultRecord.Type.RETRACT),
                "old sealed result retracted");
        assertTrue(types(records).contains(ResultRecord.Type.SEALED),
                "session re-sealed (end still 10 <= wm)");
    }

    @Test
    void eventBeyondAllowedLatenessIsDroppedAndStateUntouched() {
        SessionWindowEngine engine = new SessionWindowEngine(10, 5);
        engine.processEvent(Event.of("u1", 0, 2.0)); // [0,10]
        engine.processWatermark(20); // sealed + purged (20-10 >= 5)
        assertEquals(0, engine.totalSessionCount(), "purged before late event");

        engine.processEvent(Event.of("u1", 12, 3.0)); // 12 < 20-5=15 -> dropped
        ResultRecord drop = firstOfType(engine.records(), ResultRecord.Type.DROPPED);
        assertEquals(12L, drop.event().timestamp(), "dropped event timestamp");
        assertEquals(0, engine.totalSessionCount(), "drop creates no state");
        assertEquals(0, engine.keyCount(), "no key entry created on drop");
    }

    @Test
    void latenessBoundaryIsInclusive() {
        // t == watermark - allowedLateness is still accepted.
        SessionWindowEngine engine = new SessionWindowEngine(10, 5);
        engine.processEvent(Event.of("u1", 0));
        engine.processWatermark(15); // 15-10=5 >=5 => purged
        engine.processEvent(Event.of("u1", 10)); // 10 == 15-5 => accepted, new session
        assertEquals(1, engine.totalSessionCount(), "boundary event accepted as new session");
        assertFalse(types(engine.records()).contains(ResultRecord.Type.DROPPED), "no drop");
    }

    // ------------------------------------------------------------------
    // State cleanup
    // ------------------------------------------------------------------

    @Test
    void flushPurgesAllState() {
        SessionWindowEngine engine = new SessionWindowEngine(5, 50);
        engine.processEvent(Event.of("a", 1));
        engine.processEvent(Event.of("a", 100));
        engine.processEvent(Event.of("b", 2));
        engine.processEvent(Event.of("b", 50));
        engine.flush();
        assertEquals(0, engine.keyCount(), "no keys retained");
        assertEquals(0, engine.totalSessionCount(), "no sessions retained");
        assertTrue(types(engine.records()).stream()
                .filter(t -> t == ResultRecord.Type.PURGED).count() >= 4, "all sessions purged");
    }

    @Test
    void retractedAddIsAbsentFromFoldedTable() {
        SessionWindowEngine engine = new SessionWindowEngine(10, 100);
        engine.processEvent(Event.of("u1", 0));
        engine.processEvent(Event.of("u1", 15));
        engine.processWatermark(25);
        engine.processEvent(Event.of("u1", 10)); // bridge
        Map<Results.WindowRef, Aggregate> table = Results.fold(engine.records());
        assertEquals(1, table.size(), "exactly one live window after folding");
        Aggregate only = table.values().iterator().next();
        assertEquals(3L, only.count(), "merged window present");
    }

    private static boolean firstSealed(SessionWindowEngine engine) {
        return !engine.snapshot().isEmpty() && engine.snapshot().get(0).sealed();
    }
}
