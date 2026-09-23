package test.stream;

import java.util.List;
import java.util.Map;

import approx.cms.SketchIncompatibleException;
import approx.cms.SketchSnapshot;
import approx.stream.EngineConfig;
import approx.stream.Event;
import approx.stream.FrequentItemsEngine;
import approx.stream.WindowResult;
import approx.time.ManualEnvironment;
import approx.time.SystemRuntime;
import test.Asserts;
import test.TestRunner;

public final class FrequentItemsEngineTest {

    private FrequentItemsEngineTest() {
    }

    public static void register(TestRunner r) {
        r.add("engine: non-rotating window accumulates everything", FrequentItemsEngineTest::testNoRotation);
        r.add("engine: virtual clock ticks rotate tumbling windows", FrequentItemsEngineTest::testScheduledRotation);
        r.add("engine: out-of-order event rotates early windows itself", FrequentItemsEngineTest::testEventDrivenRotation);
        r.add("engine: late events are dropped and counted", FrequentItemsEngineTest::testLateDropped);
        r.add("engine: flush closes current window", FrequentItemsEngineTest::testFlush);
        r.add("engine: flush on rotating window closes at the boundary",
                FrequentItemsEngineTest::testFlushRotating);
        r.add("engine: query reports estimate, true count and bound", FrequentItemsEngineTest::testQuery);
        r.add("engine: merge compatible snapshot over HTTP-engine path", FrequentItemsEngineTest::testMerge);
        r.add("engine: merge rejects different seed snapshot", FrequentItemsEngineTest::testMergeRejectSeed);
        r.add("engine: system runtime wiring works without injected mocks",
                FrequentItemsEngineTest::testSystemRuntime);
    }

    private static EngineConfig cfg(boolean exact, long windowMillis) {
        return EngineConfig.builder()
                .sketchDimensions(512, 5)
                .seed(11L)
                .candidateCapacity(5)
                .windowMillis(windowMillis)
                .trackExact(exact)
                .build();
    }

    private static void testNoRotation() {
        ManualEnvironment env = new ManualEnvironment(1000L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(true, 0L), env, env)) {
            e.start();
            e.addEvent(new Event<>("a", 1000L));
            e.addEvent(new Event<>("a", 2000L));
            e.addEvent(new Event<>("b", 3000L));
            env.advance(1_000_000L);
            Asserts.assertEquals(0, e.closedWindows().size(), "no rotation with windowMillis=0");
            Asserts.assertEquals(3L, e.stats().get("activeWindowTotal"), "all in one window");
        }
    }

    private static void testScheduledRotation() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(true, 1000L), env, env)) {
            e.start();
            for (int i = 0; i < 10; i++) {
                e.addEvent(new Event<>("a", i));
            }
            // All events stay inside window 0 ([0,1000)); no rotation until a tick.
            Asserts.assertEquals(0, e.closedWindows().size(), "nothing closed before a boundary tick");
            env.advance(2000L);
            Asserts.assertEquals(2, e.closedWindows().size(), "two windows closed after t=2000");
            WindowResult w0 = e.closedWindows().get(0);
            Asserts.assertEquals(0L, w0.windowStartMillis, "w0 start");
            Asserts.assertEquals(1000L, w0.windowEndMillis, "w0 end");
            Asserts.assertEquals(10L, w0.totalCount, "w0 total");
        }
    }

    private static void testEventDrivenRotation() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(false, 100L), env, env)) {
            e.start();
            e.addEvent(new Event<>("early", 10L));
            // Event at t=250 belongs to window 2 ([200,300)), so windows 0 and 1
            // close; window 1 in between is emitted empty.
            e.addEvent(new Event<>("late", 250L));
            Asserts.assertEquals(2, e.closedWindows().size(), "windows 0,1 closed");
            Asserts.assertEquals(100L, e.closedWindows().get(1).windowStartMillis, "window 1 start");
            WindowResult w2 = e.closedWindows().get(1);
            Asserts.assertEquals(0L, w2.totalCount, "window 1 empty");
            Asserts.assertEquals(1L, e.stats().get("activeWindowTotal"), "event lands in active window 2");
        }
    }

    private static void testLateDropped() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(false, 100L), env, env)) {
            e.start();
            e.addEvent(new Event<>("on-time", 50L));
            e.addEvent(new Event<>("future", 250L));
            FrequentItemsEngine.AddResult r = e.addEvent(new Event<>("late", 50L));
            Asserts.assertEquals(0L, r.accepted, "late not accepted");
            Asserts.assertEquals(1L, r.droppedLate, "late counted");
            Asserts.assertEquals(1L, e.stats().get("droppedLateTotal"), "late total");
        }
    }

    private static void testFlush() {
        ManualEnvironment env = new ManualEnvironment(1000L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(true, 0L), env, env)) {
            e.start();
            e.addEvent(new Event<>("x", 1000L));
            WindowResult r = e.flush();
            Asserts.assertEquals(1L, r.totalCount, "flushed window total");
            Asserts.assertEquals(1, e.closedWindows().size(), "one closed window");
            Asserts.assertEquals(0L, e.stats().get("activeWindowTotal"), "new active window empty");
        }
    }

    private static void testFlushRotating() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(true, 1000L), env, env)) {
            e.start();
            e.addEvent(new Event<>("a", 100L));
            // At virtual time 200, flush closes window 0 early at its boundary 1000;
            // no events are lost and the active window becomes [1000,2000).
            env.advance(200L);
            WindowResult r = e.flush();
            Asserts.assertEquals(0L, r.windowStartMillis, "flushed window starts at boundary");
            Asserts.assertEquals(1000L, r.windowEndMillis, "flushed window ends at boundary");
            Asserts.assertEquals(1L, r.totalCount, "event preserved in flushed window");
            Asserts.assertEquals(1, e.closedWindows().size(), "one window closed");
            // An event at 1500 now lands in the active window without extra rotation.
            e.addEvent(new Event<>("b", 1500L));
            Asserts.assertEquals(1, e.closedWindows().size(), "no extra rotation");
            Asserts.assertEquals(1L, e.stats().get("activeWindowTotal"), "b in active window");
        }
    }

    private static void testQuery() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine e = new FrequentItemsEngine("e", cfg(true, 0L), env, env)) {
            e.start();
            for (int i = 0; i < 100; i++) {
                e.addEvent(new Event<>("hot", i));
            }
            Map<String, Object> q = e.queryItem("hot");
            Asserts.assertEquals(100L, q.get("estimate"), "exact with wide sketch");
            Asserts.assertEquals(100L, q.get("trueCount"), "true count tracked");
            // bound = ceil(e/512 * 100) = ceil(0.53) = 1; the *actual* error is zero.
            Asserts.assertEquals(1L, q.get("errorUpperBound"), "declared bound = ceil(epsilon*N)");
            Asserts.assertEquals(0L, (long) q.get("estimate") - (long) q.get("trueCount"),
                    "actual error zero on this data");
        }
    }

    private static void testMerge() {
        ManualEnvironment env = new ManualEnvironment(0L);
        try (FrequentItemsEngine a = new FrequentItemsEngine("a", cfg(false, 0L), env, env);
                FrequentItemsEngine b = new FrequentItemsEngine("b", cfg(false, 0L), env, env)) {
            a.start();
            b.start();
            for (int i = 0; i < 5; i++) {
                a.addEvent(new Event<>("shared", 0L));
                b.addEvent(new Event<>("shared", 0L));
            }
            SketchSnapshot snap = b.activeSnapshot();
            a.mergeSketch(snap);
            Asserts.assertEquals(10L, a.queryItem("shared").get("estimate"), "merged estimate");
        }
    }

    private static void testMergeRejectSeed() {
        ManualEnvironment env = new ManualEnvironment(0L);
        EngineConfig other = EngineConfig.builder()
                .sketchDimensions(512, 5).seed(999L).candidateCapacity(5).build();
        try (FrequentItemsEngine a = new FrequentItemsEngine("a", cfg(false, 0L), env, env);
                FrequentItemsEngine b = new FrequentItemsEngine("b", other, env, env)) {
            a.start();
            b.start();
            a.addEvent(new Event<>("x", 0L));
            b.addEvent(new Event<>("x", 0L));
            try {
                a.mergeSketch(b.activeSnapshot());
                Asserts.fail("seed mismatch must reject merge");
            } catch (SketchIncompatibleException expected) {
                // expected
            }
        }
    }

    private static void testSystemRuntime() throws Exception {
        try (SystemRuntime runtime = new SystemRuntime("test-runtime")) {
            EngineConfig c = EngineConfig.builder()
                    .sketchDimensions(64, 3).candidateCapacity(3).windowMillis(60_000L).build();
            try (FrequentItemsEngine e = new FrequentItemsEngine("sys", c, runtime, runtime)) {
                e.start();
                long now = runtime.nowMillis();
                e.addEvent(new Event<>("z", now));
                Asserts.assertEquals(1L, e.stats().get("activeWindowTotal"), "event accepted on system time");
            }
        }
    }
}
