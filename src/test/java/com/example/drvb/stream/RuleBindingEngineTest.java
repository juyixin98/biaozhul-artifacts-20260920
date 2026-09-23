package com.example.drvb.stream;

import com.example.drvb.core.Condition;
import com.example.drvb.core.Event;
import com.example.drvb.core.Rule;
import com.example.drvb.core.RuleRegistry;
import com.example.drvb.core.RuleVersion;
import com.example.drvb.time.SimClock;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RuleBindingEngineTest {

    // Timeline (event-time epoch seconds for readability):
    //   v1 bootstrap: amount >= 100  -> BIG (covers -inf .. 1000)
    //   v2 from 1000: amount >= 200  -> BIG  (covers 1000 .. 2000)
    //   v3 from 2000: amount >= 300  -> BIG  (covers 2000 ..)
    private static RuleVersion v(String id, long from, long createdAt, int threshold) {
        Rule r = new Rule("big", "big-amount", "payment",
                new Condition.Compare("gte", "amount", threshold), "BLOCK", true);
        return new RuleVersion(id, from, createdAt, id, List.of(r));
    }

    private static Event ev(String id, long time, int amount) {
        return new Event(id, "payment", time * 1000L, Map.of("amount", amount));
    }

    private RuleBindingEngine engine(SimClock clock) {
        // wm delay 0 (watermark == max seen event time), lateness 2000s,
        // retention 10_000s for predictable reference-run semantics.
        return new RuleBindingEngine(new RuleRegistry(),
                new WatermarkTracker(0), new InMemoryResultStore(),
                clock, 2_000_000L, 10_000_000L);
    }

    @Test
    void missingVersionRejectedBeforeBootstrapNeverLatestSilently() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        IngestResult r = eng.ingest(ev("e0", 500, 999));
        assertFalse(r.accepted());
        assertEquals(RuleBindingEngine.MISSING_VERSION, r.rejectionReason());

        // bootstrap v1; the pre-bootstrap event, if replayed, still belongs to
        // v1 interval now (bootstrap covers history) — but it was already
        // rejected and recorded as such.
        eng.bootstrap(v("v1", Long.MIN_VALUE, 2L, 100));
        IngestResult r2 = eng.ingest(ev("e1", 500, 99));
        assertTrue(r2.accepted());
        assertEquals("v1", r2.version().versionId());
        assertTrue(r2.matches().isEmpty(), "99 < 100 -> no match under v1");
    }

    @Test
    void lateEventsBindToHistoricalVersionNotLatest() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));

        // push the watermark far ahead with on-time events + versions
        eng.publish(v("v2", 1_000_000L, 2L, 200));
        eng.publish(v("v3", 2_000_000L, 3L, 300));
        IngestResult ahead = eng.ingest(ev("ahead", 2_500, 350));
        assertTrue(ahead.accepted());
        assertEquals("v3", ahead.version().versionId());
        assertEquals(2_500_000L, ahead.watermark());

        // late but within horizon: event at t=1500 must use v2 (>=200),
        // NOT v3 (>=300). amount 250 matches only under v2.
        IngestResult late = eng.ingest(ev("late-1500", 1_500, 250));
        assertTrue(late.accepted(), "late events are still accepted");
        assertTrue(late.late());
        assertEquals("v2", late.version().versionId());
        assertEquals(List.of("big"), late.matchedRuleIds());

        // even later: t=500 binds to v1; amount 150 matches only under v1
        IngestResult lateV1 = eng.ingest(ev("late-500", 500, 150));
        assertTrue(lateV1.accepted());
        assertEquals("v1", lateV1.version().versionId());
        assertEquals(List.of("big"), lateV1.matchedRuleIds());
    }

    @Test
    void boundaryInstantsBelongToNewVersion() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.publish(v("v2", 1_000_000L, 2L, 200));

        // boundary event exactly at t=1000 arrives first, before watermark moves
        IngestResult atBoundary = eng.ingest(ev("at", 1_000, 250));
        assertEquals("v2", atBoundary.version().versionId(),
                "interval is [from,next): boundary belongs to the new version");

        // one instant before binds v1
        IngestResult justBefore = eng.ingest(ev("before", 999, 150));
        assertEquals("v1", justBefore.version().versionId());
    }

    @Test
    void tooLateAndReclaimedAreRejectedWithDistinctReasons() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.publish(v("v2", 1_000_000L, 2L, 200));
        eng.ingest(ev("tip", 5_000, 999)); // watermark -> 5_000_000

        // beyond the 2000s lateness horizon but version still retained => TOO_LATE
        IngestResult veryLate = eng.ingest(ev("vl", 2_500, 999));
        assertFalse(veryLate.accepted());
        assertEquals(RuleBindingEngine.TOO_LATE, veryLate.rejectionReason());

        // GC at horizon 3_000_000: v1's successor starts at 1_000_000 <=
        // horizon and no references remain (purge old results first) => gone
        clock.set(20_000_000L);
        RuleBindingEngine.GcReport report = eng.runMaintenance();
        assertEquals(1, report.versionsReclaimed());
        assertEquals("v1", report.removed().get(0).versionId());

        // event in v1's old interval is now VERSION_RECLAIMED, even though it
        // is also beyond the horizon: the precise cause takes precedence and
        // the latest rules are never silently substituted.
        IngestResult ancient = eng.ingest(ev("ancient", 500, 150));
        assertFalse(ancient.accepted());
        assertEquals(RuleBindingEngine.VERSION_RECLAIMED,
                ancient.rejectionReason());
    }

    @Test
    void referencedVersionIsProtectedFromGc() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.publish(v("v2", 1_000_000L, 2L, 200));
        // an accepted result pinned to v1
        IngestResult pinned = eng.ingest(ev("old", 500, 150));
        assertEquals("v1", pinned.version().versionId());
        eng.ingest(ev("tip", 5_000, 10)); // wm -> 5_000_000

        clock.set(10L); // within result retention: results survive
        RuleBindingEngine.GcReport report = eng.runMaintenance();
        assertEquals(0, report.versionsReclaimed(),
                "v1 referenced by a retained result -> not collectible");
        assertEquals(2, report.retainedVersions().size());

        // after retention passes, results purge first; the same GC then frees v1
        clock.set(11_000_000L);
        RuleBindingEngine.GcReport report2 = eng.runMaintenance();
        assertTrue(report2.resultsPurged() >= 1);
        assertEquals(1, report2.versionsReclaimed());
        assertEquals(List.of("v2"),
                report2.retainedVersions().stream().map(RuleVersion::versionId).toList());
    }

    @Test
    void rollbackRestoresContentUnderNewVersion() {
        SimClock clock = new SimClock(10L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.publish(v("v2", 1_000_000L, 2L, 200));
        eng.publish(v("v3", 2_000_000L, 3L, 300));
        eng.ingest(ev("tip", 2_500, 10));

        // rollback to v1 content effective from t=3000
        RuleVersion rb = eng.rollback("v1", "rb1", 3_000_000L, "undo threshold change");
        assertEquals(eng.registry().get("v1").checksum(), rb.checksum());

        IngestResult r = eng.ingest(ev("after-rb", 3_000, 150));
        assertEquals("rb1", r.version().versionId());
        assertEquals(List.of("big"), r.matchedRuleIds(), "threshold 100 restored");

        // v3 content is still immutable for its own historical interval
        IngestResult late = eng.ingest(ev("late", 2_500, 250));
        assertEquals("v3", late.version().versionId());
        assertTrue(late.matches().isEmpty(), "250 < 300 under immutable v3");
    }

    @Test
    void versionsMayBePreRegisteredButIntervalsMustAdvance() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.ingest(ev("tip", 2_000, 10)); // wm 2_000_000
        // pre-registering a future version is allowed despite the watermark ...
        eng.publish(v("v2", 5_000_000L, 2L, 50));
        // ... but an event at 1500 still binds v1 (future version invisible)
        IngestResult mid = eng.ingest(ev("mid", 1_500, 75));
        assertEquals("v1", mid.version().versionId());
        assertEquals(List.of(), mid.matchedRuleIds(), "75 < 100 under v1");
        // and intervals can never go backwards or overlap
        assertThrows(com.example.drvb.core.RuleRegistryException.class,
                () -> eng.publish(v("vX", 5_000_000L, 3L, 25)));
        assertThrows(com.example.drvb.core.RuleRegistryException.class,
                () -> eng.publish(v("vY", 4_999_999L, 3L, 25)));
    }

    @Test
    void interleavedUpdatesAndOutOfOrderEventsScenario() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));

        // interleaving: event, update, out-of-order late event, update ...
        IngestResult a = eng.ingest(ev("a", 100, 150));          // v1 match
        eng.publish(v("v2", 1_000_000L, 2L, 200));
        IngestResult b = eng.ingest(ev("b", 1_200, 150));        // v2 no match
        IngestResult c = eng.ingest(ev("c", 900, 150));          // late -> v1 match
        eng.publish(v("v3", 2_000_000L, 3L, 300));
        IngestResult d = eng.ingest(ev("d", 2_100, 350));        // v3 match
        IngestResult e = eng.ingest(ev("e", 1_100, 250));        // late -> v2 match

        assertEquals(List.of("v1", "v2", "v1", "v3", "v2"),
                List.of(a, b, c, d, e).stream()
                        .map(x -> x.version().versionId()).toList());
        assertEquals(List.of("big"), a.matchedRuleIds());
        assertTrue(b.matches().isEmpty());
        assertEquals(List.of("big"), c.matchedRuleIds());
        assertEquals(List.of("big"), d.matchedRuleIds());
        assertEquals(List.of("big"), e.matchedRuleIds());
        assertTrue(c.late(), "c(t=900) arrives after watermark reached 1200");
        assertTrue(e.late());
        assertFalse(a.late() || b.late() || d.late());
    }

    @Test
    void forcedWatermarkAndStatsCounters() {
        SimClock clock = new SimClock(1L);
        RuleBindingEngine eng = engine(clock);
        IngestResult r = eng.ingest(ev("pre", 1, 1));
        assertEquals(RuleBindingEngine.MISSING_VERSION, r.rejectionReason());

        eng.bootstrap(v("v1", Long.MIN_VALUE, 1L, 100));
        eng.advanceWatermark(10_000_000L);
        IngestResult tooLate = eng.ingest(ev("old", 5, 150));
        assertEquals(RuleBindingEngine.TOO_LATE, tooLate.rejectionReason());

        Map<String, Object> stats = eng.stats();
        assertEquals(0L, stats.get("accepted"));
        @SuppressWarnings("unchecked")
        Map<String, Object> rejected = (Map<String, Object>) stats.get("rejected");
        assertEquals(2L, rejected.get("total"));
        assertEquals(1L, rejected.get(RuleBindingEngine.MISSING_VERSION));
        assertEquals(1L, rejected.get(RuleBindingEngine.TOO_LATE));
        assertEquals(0L, rejected.get(RuleBindingEngine.VERSION_RECLAIMED));
    }
}
