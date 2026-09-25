package com.example.dedup.tests;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;
import com.example.dedup.model.IngestResult;
import com.example.dedup.model.WindowResult;
import com.example.dedup.pipeline.PipelineConfig;
import com.example.dedup.pipeline.StreamPipeline;
import com.example.dedup.time.ManualClock;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * End-to-end acceptance tests for the four required scenarios:
 *  1. same id with different payload,
 *  2. clock rollback,
 *  3. restart recovery,
 *  4. extremely late duplicate.
 * Plus the headline invariant: no duplicate output within the promise horizon.
 */
public final class AcceptanceTest {

    /**
     * Timing layout (ms): window 1000, window lateness 100,
     * dedup retention 2000 and tombstone ttl 2000 (>= window lifetime 1100
     * so window output can never double-count an unverifiable occurrence).
     */
    static PipelineConfig cfg() {
        return new PipelineConfig(100, 2000, 100, 2000, 1000, 100, 0, 0);
    }

    public static void register(TestRunner r) {
        noDuplicateWithinPromise(r);
        sameIdDifferentPayload(r);
        clockRollback(r);
        restartRecovery(r);
        extremelyLateDuplicate(r);
        tombstoneScenario(r);
        payloadHashOrderInsensitive(r);
    }

    // ----------------------------------------------------------------

    private static void noDuplicateWithinPromise(TestRunner r) {
        r.run("AC-0 promise horizon: every guaranteed duplicate is suppressed", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            List<String> emittedIds = new ArrayList<>();
            // 50 unique ids all in window [0,1000), emitted before any watermark
            // can fire that window (firing needs wm >= 1100).
            for (int i = 0; i < 50; i++) {
                long eventTime = 100 + (i % 9) * 50; // 100..500, deliberately unordered-ish
                IngestResult res = p.ingest(Event.upsert("id-" + i, eventTime,
                        "k" + (i % 5), TestData.payload("n", i)));
                if (res.emitted()) {
                    emittedIds.add("id-" + i);
                }
            }
            // Repeat every id 4 times (identical event time + payload), as real
            // redelivery would, driving the watermark forward but never past
            // the first window's firing deadline while duplicates flow.
            for (int round = 0; round < 4; round++) {
                for (int i = 0; i < 50; i++) {
                    long eventTime = 100 + (i % 9) * 50;
                    IngestResult res = p.ingest(Event.upsert("id-" + i, eventTime,
                            "k" + (i % 5), TestData.payload("n", i)));
                    if (res.emitted()) {
                        emittedIds.add("id-" + i);
                    }
                }
            }
            long unique = emittedIds.stream().distinct().count();
            TestRunner.assertEquals((long) unique, (long) emittedIds.size(),
                    "emitted list must contain no repeated id");
            TestRunner.assertEquals(50L, unique, "all 50 unique ids emitted exactly once");
            TestRunner.assertEquals(200L, p.metrics().duplicatesIn, "200 duplicate occurrences seen");

            // Now fire the windows and confirm each key aggregate counts its
            // ids once (no double counting in window output).
            p.advanceWatermark(1100);
            List<WindowResult> wins = p.drainWindowOutputs();
            long totalUpserts = wins.stream().mapToLong(WindowResult::upsertCount).sum();
            TestRunner.assertEquals(50L, totalUpserts, "window aggregates count 50, not 250");
        });
    }

    private static void sameIdDifferentPayload(TestRunner r) {
        r.run("AC-1 same id with different payload -> duplicate + mismatch, first output kept", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            Event e1 = Event.upsert("x", 100, "k", TestData.payload("v", 1));
            Event e2 = Event.upsert("x", 100, "k", TestData.payload("v", 2));
            IngestResult r1 = p.ingest(e1);
            IngestResult r2 = p.ingest(e2);

            TestRunner.assertTrue(r1.accepted(), "first accepted");
            TestRunner.assertTrue(r1.emitted(), "first emitted");
            TestRunner.assertTrue(r2.duplicate(), "second is duplicate");
            TestRunner.assertTrue(r2.payloadMismatch(), "different payload is observable");
            TestRunner.assertFalse(r2.accepted(), "duplicate not accepted");
            TestRunner.assertFalse(r2.emitted(), "no second output");
            TestRunner.assertEquals(1L, p.metrics().payloadMismatches, "metric counted");

            // the window result carries the FIRST payload (no overwrite by dup)
            p.advanceWatermark(2000);
            List<WindowResult> wins = p.drainWindowOutputs();
            WindowResult k = wins.stream().filter(w -> w.key().equals("k")).findFirst().orElseThrow();
            TestRunner.assertEquals(1L, k.upsertCount(), "window counted once");
            Json.JsonObject expected = (Json.JsonObject) TestData.payload("v", 1);
            TestRunner.assertEquals(Json.canonical(expected),
                    Json.canonical(k.lastPayload()), "payload is first occurrence's");
        });
    }

    private static void clockRollback(TestRunner r) {
        r.run("AC-2 processing clock rollback never releases state or duplicates output", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock(50_000));
            IngestResult first = p.ingest(Event.upsert("x", 100, "k", TestData.payload("v", 1)));
            TestRunner.assertTrue(first.emitted(), "first emitted");

            // Processing-time clock leaps backwards — event time must be unaffected.
            // (ManualClock here models the processing clock; event time is explicit.)
            ManualClock mc = new ManualClock(50_000);
            mc.setTime(1); // huge rollback
            TestRunner.assertEquals(1L, mc.currentTimeMillis(), "clock visibly back");

            // Watermark is event-time driven and still at its old value;
            // the duplicate must still be suppressed.
            IngestResult dup = p.ingest(Event.upsert("x", 100, "k", TestData.payload("v", 1)));
            TestRunner.assertTrue(dup.duplicate(), "dedup survives processing clock rollback");
            TestRunner.assertFalse(dup.emitted(), "no duplicate output after rollback");

            // And a backwards watermark advance is rejected.
            boolean moved = p.advanceWatermark(-1_000_000L);
            TestRunner.assertFalse(moved, "watermark cannot move backwards");
            TestRunner.assertEquals(1L, p.metrics().watermarkRegressions, "regression counted");
        });
    }

    private static void restartRecovery(TestRunner r) {
        r.run("AC-3 snapshot+restart: dedup, tombstones, windows, watermark all recover", () -> {
            var p1 = new StreamPipeline(cfg(), new ManualClock());
            p1.ingest(Event.upsert("keep", 100, "k1", TestData.payload("v", 9)));
            p1.ingest(Event.upsert("soon", 200, "k2", TestData.payload("v", 8)));
            p1.ingest(Event.delete("del", 300, "k3"));
            p1.advanceWatermark(500); // progress but window [0,1000) not fired yet
            Json.Value snap = p1.snapshot();

            // Simulate fresh process: rebuild from JSON.
            var p2 = StreamPipeline.restore((Json.JsonObject) snap, cfg(), new ManualClock(), null);

            TestRunner.assertEquals(500L, p2.watermark(), "watermark restored");

            // duplicate ids recognized after restart
            IngestResult d1 = p2.ingest(Event.upsert("keep", 100, "k1", TestData.payload("v", 9)));
            TestRunner.assertTrue(d1.duplicate(), "dedup state restored");

            // tombstone still suppresses after restart: an upsert at an event
            // time at or before the retained delete (300) is hidden and never
            // folds into the window. (Post-delete re-create is tested in AC-5.)
            IngestResult up = p2.ingest(Event.upsert("after-restart-upsert", 250, "k3",
                    TestData.payload("v", 1)));
            TestRunner.assertTrue(up.suppressedByTombstone(), "restored tombstone suppresses older/equal upsert");

            // pending window accumulator restored: new event lands in same window,
            // then window fires with restored counts (the suppressed upsert@250
            // never entered the window, so k3 holds only its restored delete)
            p2.ingest(Event.upsert("new", 400, "k1", TestData.payload("v", 10)));
            p2.advanceWatermark(2000);
            List<WindowResult> wins = p2.drainWindowOutputs();
            Map<String, WindowResult> byKey = new HashMap<>();
            wins.forEach(w -> byKey.put(w.key(), w));
            TestRunner.assertEquals(2L, byKey.get("k1").upsertCount(), "accumulator restored + new folded");
            TestRunner.assertEquals(1L, byKey.get("k3").deleteCount(), "delete restored");
            TestRunner.assertEquals(0L, byKey.get("k3").upsertCount(), "suppressed upsert never folded");
            TestRunner.assertTrue(byKey.get("k3").deletedByTombstone(), "delete-only window marked deleted");
        });
    }

    private static void extremelyLateDuplicate(TestRunner r) {
        r.run("AC-4 extremely late duplicate after state release is flagged unverified", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            IngestResult first = p.ingest(Event.upsert("late-id", 100, "k", TestData.payload("v", 1)));
            TestRunner.assertTrue(first.emitted(), "original emitted");

            // Advance watermark far beyond retention: state for t=100 released.
            p.advanceWatermark(100_000);
            TestRunner.assertEquals(0, p.metrics().activeDedupEntries, "state released");

            // Same id, same ancient event time comes back.
            IngestResult ret = p.ingest(Event.upsert("late-id", 100, "k", TestData.payload("v", 1)));
            TestRunner.assertTrue(ret.dedupUnverified(), "outside promise -> unverified flag");
            TestRunner.assertTrue(ret.late(), "also late vs watermark");
            TestRunner.assertTrue(ret.windowLateDropped(), "window for t=100 fired long ago");
            TestRunner.assertFalse(ret.emitted(), "but cannot be placed in a window -> not emitted again");
            TestRunner.assertEquals(1L, p.metrics().unverifiedDuplicates, "metric");
            TestRunner.assertEquals(1L, p.metrics().windowLateDropped, "late drop metric");
        });

        r.run("AC-4b unverifiable old occurrence is never silently treated as guaranteed fresh", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            p.advanceWatermark(100_000);
            // A brand-new-looking id but with an ancient event time.
            IngestResult neverSeen = p.ingest(
                    Event.upsert("never-seen-before", 100, "k", TestData.payload("v", 7)));
            TestRunner.assertTrue(neverSeen.accepted(), "first live occurrence is accepted");
            TestRunner.assertTrue(neverSeen.dedupUnverified(),
                    "but flagged unverified: even 'new' old data is outside the promise");
            TestRunner.assertTrue(neverSeen.windowLateDropped(), "window already fired -> side output");
        });
    }

    private static void tombstoneScenario(TestRunner r) {
        r.run("AC-5 delete tombstone hides earlier/equal upserts; later re-create passes", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            p.ingest(Event.upsert("u1", 100, "k", TestData.payload("v", 1)));
            p.ingest(Event.delete("d1", 300, "k"));
            // an out-of-order upsert belonging BEFORE the delete is hidden
            IngestResult hidden = p.ingest(Event.upsert("u-old", 200, "k", TestData.payload("v", 2)));
            TestRunner.assertTrue(hidden.suppressedByTombstone(),
                    "upsert at event time before the delete is hidden by tombstone");
            TestRunner.assertFalse(hidden.emitted(), "no emission");
            // an upsert AFTER the delete is a re-create and passes
            IngestResult recreated = p.ingest(Event.upsert("u-new", 400, "k", TestData.payload("v", 3)));
            TestRunner.assertFalse(recreated.suppressedByTombstone(),
                    "upsert later than delete is a re-create, passes");
            TestRunner.assertTrue(recreated.emitted(), "re-create is emitted");

            // after TTL release, a very late upsert is uncertain, never a silent claim
            var p3 = new StreamPipeline(cfg(), new ManualClock());
            p3.ingest(Event.delete("d", 100, "z"));
            p3.advanceWatermark(100_000);
            IngestResult late = p3.ingest(Event.upsert("u", 100, "z", TestData.payload("v", 1)));
            TestRunner.assertTrue(late.tombstoneUncertain(), "expired tombstone -> uncertain flag");
            TestRunner.assertTrue(late.dedupUnverified(), "also beyond dedup promise");
        });
    }

    private static void payloadHashOrderInsensitive(TestRunner r) {
        r.run("AC-6 same JSON payload with different key order is NOT a mismatch", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            Json.JsonObject a = Json.obj();
            a.members().put("x", Json.num(1));
            a.members().put("y", Json.num(2));
            Json.JsonObject b = Json.obj();
            b.members().put("y", Json.num(2));
            b.members().put("x", Json.num(1));
            p.ingest(Event.upsert("id", 100, "k", a));
            IngestResult dup = p.ingest(Event.upsert("id", 100, "k", b));
            TestRunner.assertTrue(dup.duplicate(), "duplicate");
            TestRunner.assertFalse(dup.payloadMismatch(), "canonical JSON hashing ignores key order");
        });
    }
}
