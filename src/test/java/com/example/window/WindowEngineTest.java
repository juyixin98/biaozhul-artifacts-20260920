package com.example.window;

import java.util.List;
import java.util.Map;

/**
 * Deterministic end-to-end checks for {@link WindowEngine}:
 * out-of-order events, window boundaries, watermark-driven close time,
 * late events within the allowed lateness (re-fire), purge, side output,
 * duplicate ids, idle-partition exclusion and recovery, and global-watermark
 * min-over-active-partitions semantics.
 */
final class WindowEngineTest {

    private WindowEngineTest() {
    }

    private static long lng(Map<String, Object> m, String key) {
        return (Long) m.get(key);
    }

    private static Map<String, Object> resultAt(WindowEngine e, int index) {
        return e.results().get(index);
    }

    static void run() {
        System.out.println("WindowEngineTest");
        deterministicSequence();
        watermarkAndIdleRules();
        boundaryWindowAssignment();
    }

    /** The main acceptance sequence: windowSize=10, allowedLateness=2. */
    private static void deterministicSequence() {
        WindowEngine e = new WindowEngine(10, 2);

        // --- Phase A: fill window [0,10), out of order ----------------------
        TestMain.eq("e1 ts=3  -> ACCEPTED",
                "ACCEPTED", e.addEvent("e1", "a", 3, "p1").get("status"));
        TestMain.eq("e2 ts=7  -> ACCEPTED",
                "ACCEPTED", e.addEvent("e2", "b", 7, "p2").get("status"));
        TestMain.eq("e3 ts=9  -> ACCEPTED",
                "ACCEPTED", e.addEvent("e3", "a", 9, "p1").get("status"));
        TestMain.eq("e10 ts=0 (left boundary) -> ACCEPTED",
                "ACCEPTED", e.addEvent("e10", "c", 0, "p2").get("status"));
        TestMain.eq("no window closes before watermark", 0, e.results().size());
        TestMain.eq("global watermark unset", Long.MIN_VALUE, e.globalWatermark());

        // --- Phase B: next-window events; ts=10 belongs to [10,20) ----------
        TestMain.eq("e7 ts=10 (right-open boundary) -> window [10,20)",
                10L, lng(e.addEvent("e7", "a", 10, "p1"), "windowStart"));
        TestMain.eq("e8 ts=19 -> window [10,20)",
                10L, lng(e.addEvent("e8", "b", 19, "p2"), "windowStart"));

        // --- Phase C: watermarks close [0,10) exactly at wm=10 --------------
        e.submitWatermark("p1", 10);
        TestMain.eq("global still unset: p2 holds the min", Long.MIN_VALUE, e.globalWatermark());
        e.submitWatermark("p2", 10);
        TestMain.eq("global watermark = 10", 10L, e.globalWatermark());
        TestMain.eq("[0,10) fires exactly once", 1, e.results().size());
        Map<String, Object> r1 = resultAt(e, 0);
        TestMain.eq("r1 windowStart", 0L, lng(r1, "windowStart"));
        TestMain.eq("r1 windowEnd", 10L, lng(r1, "windowEnd"));
        TestMain.eq("r1 count = 4", 4L, lng(r1, "count"));
        TestMain.eq("r1 closedAtWatermark = 10", 10L, lng(r1, "closedAtWatermark"));
        TestMain.eq("r1 not final yet", false, r1.get("final"));
        TestMain.eq("byKey a=2", 2L, lng(Json.asObject(r1.get("byKey")), "a"));
        TestMain.eq("byKey b=1", 1L, lng(Json.asObject(r1.get("byKey")), "b"));
        TestMain.eq("byKey c=1", 1L, lng(Json.asObject(r1.get("byKey")), "c"));
        TestMain.eq("[10,20) still open (10 < 20); nothing purged", 0, e.purges().size());

        // --- Phase D: late within tolerance -> re-fire; duplicates ----------
        TestMain.eq("e4 ts=5 late but within tolerance -> ACCEPTED",
                "ACCEPTED", e.addEvent("e4", "a", 5, "p1").get("status"));
        TestMain.eq("e4 repeated -> DUPLICATE",
                "DUPLICATE", e.addEvent("e4", "a", 5, "p1").get("status"));
        TestMain.eq("duplicate counter", 1L, e.duplicateCount());
        e.submitWatermark("p1", 11);
        TestMain.eq("p2 still 10: global unchanged", 10L, e.globalWatermark());
        e.submitWatermark("p2", 11);
        TestMain.eq("global watermark = 11", 11L, e.globalWatermark());
        TestMain.eq("[0,10) re-fired with updated count", 2, e.results().size());
        Map<String, Object> r2 = resultAt(e, 1);
        TestMain.eq("r2 count = 5", 5L, lng(r2, "count"));
        TestMain.eq("r2 closedAtWatermark = 11", 11L, lng(r2, "closedAtWatermark"));
        TestMain.eq("r2 not final yet", false, r2.get("final"));

        // --- Phase E: tolerance expires at wm=12 -> purge; then side output --
        e.submitWatermark("p1", 12);
        e.submitWatermark("p2", 12);
        TestMain.eq("global watermark = 12", 12L, e.globalWatermark());
        TestMain.eq("[0,10) purged exactly once", 1, e.purges().size());
        Map<String, Object> pu1 = e.purges().get(0);
        TestMain.eq("purge start", 0L, lng(pu1, "windowStart"));
        TestMain.eq("purge end", 10L, lng(pu1, "windowEnd"));
        TestMain.eq("purge watermark", 12L, lng(pu1, "purgedAtWatermark"));
        TestMain.eq("purge finalCount = 5", 5L, lng(pu1, "finalCount"));
        TestMain.eq("latest emission flagged final", true, r2.get("final"));
        TestMain.eq("no extra emission on purge (no change)", 2, e.results().size());

        TestMain.eq("e6 ts=6 after purge -> LATE_SIDE_OUTPUT",
                "LATE_SIDE_OUTPUT", e.addEvent("e6", "a", 6, "p1").get("status"));
        TestMain.eq("side output size 1", 1, e.sideOutput().size());
        Map<String, Object> s1 = e.sideOutput().get(0);
        TestMain.eq("side output eventId", "e6", s1.get("eventId"));
        TestMain.eq("side output windowStart", 0L, lng(s1, "windowStart"));

        // --- Phase F: idle partition excluded, then recovers ----------------
        e.markIdle("p1");
        TestMain.eq("idling p1 does not lower/raise the global wm", 12L, e.globalWatermark());
        e.submitWatermark("p2", 25);
        TestMain.eq("p1 idle: global = p2 watermark = 25", 25L, e.globalWatermark());
        TestMain.eq("[10,20) fires at wm=25", 3, e.results().size());
        Map<String, Object> r3 = resultAt(e, 2);
        TestMain.eq("r3 windowStart", 10L, lng(r3, "windowStart"));
        TestMain.eq("r3 count = 2", 2L, lng(r3, "count"));
        TestMain.eq("r3 closedAtWatermark = 25", 25L, lng(r3, "closedAtWatermark"));
        TestMain.eq("r3 final immediately (wm jumped past tolerance)", true, r3.get("final"));
        TestMain.eq("[10,20) purged (25 >= 20+2)", 2, e.purges().size());

        // p1 comes back: an event revives it; its old watermark 12 cannot move
        // the global minimum above p2's 25.
        TestMain.eq("e11 ts=26 revives idle p1 -> ACCEPTED",
                "ACCEPTED", e.addEvent("e11", "a", 26, "p1").get("status"));
        TestMain.eq("global stays 25 after revival (min over active)",
                25L, e.globalWatermark());
        e.submitWatermark("p1", 30);
        TestMain.eq("p2 still 25: global unchanged", 25L, e.globalWatermark());
        e.submitWatermark("p2", 30);
        TestMain.eq("global = 30", 30L, e.globalWatermark());
        TestMain.eq("[20,30) fires at wm=30", 4, e.results().size());
        Map<String, Object> r4 = resultAt(e, 3);
        TestMain.eq("r4 windowStart", 20L, lng(r4, "windowStart"));
        TestMain.eq("r4 windowEnd", 30L, lng(r4, "windowEnd"));
        TestMain.eq("r4 count = 1", 1L, lng(r4, "count"));
        TestMain.eq("r4 closedAtWatermark = 30", 30L, lng(r4, "closedAtWatermark"));
        TestMain.eq("r4 not final (30 < 32)", false, r4.get("final"));
        TestMain.eq("[20,30) not purged yet (30 < 32)", 2, e.purges().size());

        // --- Final counters --------------------------------------------------
        TestMain.eq("accepted total = 8", 8L, e.acceptedCount());
        TestMain.eq("duplicates total = 1", 1L, e.duplicateCount());
        TestMain.eq("side output total = 1", 1, e.sideOutput().size());
    }

    /** Watermark regression, single-partition min, all-partitions-idle. */
    private static void watermarkAndIdleRules() {
        WindowEngine e = new WindowEngine(10, 0);
        TestMain.expectThrows("watermark regression rejected",
                () -> {
                    e.submitWatermark("p", 10);
                    e.submitWatermark("p", 9);
                });
        TestMain.eq("accepted watermark 10", 10L, e.globalWatermark());
        TestMain.eq("equal watermark accepted (non-decreasing)",
                10L, lng(e.submitWatermark("p", 10), "partitionWatermark"));

        // single active partition sets the global watermark directly
        WindowEngine e2 = new WindowEngine(10, 0);
        e2.submitWatermark("solo", 15);
        TestMain.eq("single partition global wm", 15L, e2.globalWatermark());

        // all partitions idle: nothing is active, no windows can close
        e2.submitWatermark("solo", 99);
        e2.markIdle("solo");
        TestMain.eq("all idle keeps the last global watermark (monotonic)",
                99L, e2.globalWatermark());

        // invalid constructor args
        TestMain.expectThrows("windowSize must be positive", () -> new WindowEngine(0, 0));
        TestMain.expectThrows("lateness must be non-negative", () -> new WindowEngine(10, -1));
    }

    /** Left-closed/right-open assignment incl. negative event times. */
    private static void boundaryWindowAssignment() {
        WindowEngine e = new WindowEngine(10, 0);
        TestMain.eq("ts=-1 -> [-10,0)",
                -10L, lng(e.addEvent("n1", "k", -1, "p"), "windowStart"));
        TestMain.eq("ts=0 -> [0,10)",
                0L, lng(e.addEvent("z", "k", 0, "p"), "windowStart"));
        TestMain.eq("ts=9 -> [0,10)",
                0L, lng(e.addEvent("nine", "k", 9, "p"), "windowStart"));
        TestMain.eq("ts=10 -> [10,20)",
                10L, lng(e.addEvent("ten", "k", 10, "p"), "windowStart"));
        TestMain.eq("ts=19 -> [10,20)",
                10L, lng(e.addEvent("nineteen", "k", 19, "p"), "windowStart"));
    }
}
