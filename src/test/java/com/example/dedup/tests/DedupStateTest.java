package com.example.dedup.tests;

import com.example.dedup.dedup.DedupState;
import com.example.dedup.watermark.WatermarkGenerator;

/** Unit tests for bounded dedup state: release horizon, unverified flag, capacity. */
public final class DedupStateTest {

    public static void register(TestRunner r) {
        r.run("dedup: first occurrence is not duplicate", () -> {
            DedupState s = new DedupState(10, 100);
            var d = s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            TestRunner.assertFalse(d.duplicate(), "first occurrence");
            TestRunner.assertFalse(d.unverified(), "no watermark -> verified zone");
        });

        r.run("dedup: same id within horizon is duplicate", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            var d = s.process("a", 1000, "h1", 1050);
            TestRunner.assertTrue(d.duplicate(), "duplicate while retained");
            TestRunner.assertFalse(d.payloadMismatch(), "same payload hash");
        });

        r.run("dedup: same id different payload is flagged mismatch", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            var d = s.process("a", 1000, "DIFFERENT", 1050);
            TestRunner.assertTrue(d.duplicate(), "duplicate");
            TestRunner.assertTrue(d.payloadMismatch(), "payload mismatch must be observable");
        });

        r.run("dedup: same id different event time is skew", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            var d = s.process("a", 1200, "h1", 1050);
            TestRunner.assertTrue(d.duplicate(), "duplicate");
            TestRunner.assertTrue(d.eventTimeSkew(), "event time differs from first");
        });

        r.run("dedup: watermark release frees state", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            // horizon: wm - retention > 1000 -> wm > 1100
            int released = s.evictExpired(1100);
            TestRunner.assertEquals(0, released, "at boundary (wm-retention == t) still retained");
            released = s.evictExpired(1101);
            TestRunner.assertEquals(1, released, "past horizon released");
            TestRunner.assertEquals(0, s.size(), "entry gone");
        });

        r.run("dedup: occurrence after release is unverified", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            s.evictExpired(1101);
            var d = s.process("a", 1000, "h1", 1101);
            TestRunner.assertFalse(d.duplicate(), "state released -> not knowably duplicate");
            TestRunner.assertTrue(d.unverified(), "must be reported unverified (outside promise)");
        });

        r.run("dedup: brand-new id at old time is also unverified past horizon", () -> {
            DedupState s = new DedupState(10, 100);
            var d = s.process("brand-new", 500, "h", 1101);
            TestRunner.assertFalse(d.duplicate(), "never seen");
            TestRunner.assertTrue(d.unverified(), "unverifiable old occurrence flagged honestly");
        });

        r.run("dedup: skew to later event time extends retention", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            s.process("a", 2000, "h1", 1050); // later duplicate
            // wm that would expire t=1000...
            TestRunner.assertEquals(0, s.evictExpired(1101), "not expired: max event time is 2000");
            TestRunner.assertEquals(1, s.evictExpired(2101), "expired once beyond 2000+100");
        });

        r.run("dedup: capacity evicts smallest event-time entry and reports it", () -> {
            DedupState s = new DedupState(2, 1_000_000);
            s.process("old", 1000, "h", WatermarkGenerator.NO_WATERMARK);
            s.process("new", 5000, "h", WatermarkGenerator.NO_WATERMARK);
            var d = s.process("third", 6000, "h", WatermarkGenerator.NO_WATERMARK);
            TestRunner.assertTrue(d.capacityEvicted(), "capacity eviction observed");
            TestRunner.assertEquals("old", d.evictedId(), "oldest event-time entry evicted");
            TestRunner.assertEquals(2, s.size(), "bound respected");
        });

        r.run("dedup: evicted entry's returning duplicate is unverified", () -> {
            DedupState s = new DedupState(1, 1_000_000);
            s.process("a", 1000, "h", WatermarkGenerator.NO_WATERMARK);
            s.process("b", 2000, "h", WatermarkGenerator.NO_WATERMARK); // evicts a
            var d = s.process("a", 1000, "h", 5000);
            TestRunner.assertTrue(d.unverified() || d.duplicate() == false,
                    "evicted id cannot be guaranteed deduped");
        });

        r.run("dedup: snapshot/restore keeps identity decisions", () -> {
            DedupState s = new DedupState(10, 100);
            s.process("a", 1000, "h1", WatermarkGenerator.NO_WATERMARK);
            s.process("b", 2000, "h2", WatermarkGenerator.NO_WATERMARK);
            var snap = s.snapshot();

            DedupState s2 = new DedupState(10, 100);
            s2.restore((com.example.dedup.json.Json.JsonObject) snap);
            var d = s2.process("a", 1000, "h1", 1050);
            TestRunner.assertTrue(d.duplicate(), "restored state still dedupes a");
            TestRunner.assertEquals(2, s2.size(), "both entries restored");
        });
    }
}
