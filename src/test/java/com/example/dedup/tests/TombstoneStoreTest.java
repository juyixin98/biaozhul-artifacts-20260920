package com.example.dedup.tests;

import com.example.dedup.tombstone.TombstoneDecision;
import com.example.dedup.tombstone.TombstoneStore;
import com.example.dedup.watermark.WatermarkGenerator;

/** Unit tests for keyed tombstones: suppression, event-time order, TTL, uncertainty. */
public final class TombstoneStoreTest {

    public static void register(TestRunner r) {
        r.run("tombstone: upsert at or before delete event time is suppressed", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            t.addDelete("k", 2000);
            TestRunner.assertEquals(TombstoneDecision.SUPPRESSED,
                    t.lookup("k", 2000, WatermarkGenerator.NO_WATERMARK), "same time covered");
            TestRunner.assertEquals(TombstoneDecision.SUPPRESSED,
                    t.lookup("k", 1500, WatermarkGenerator.NO_WATERMARK), "earlier upsert covered");
        });

        r.run("tombstone: upsert AFTER delete time is a re-create, not suppressed", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            t.addDelete("k", 2000);
            TestRunner.assertEquals(TombstoneDecision.NONE,
                    t.lookup("k", 2500, WatermarkGenerator.NO_WATERMARK),
                    "event-time ordering: a delete cannot hide a later re-create");
        });

        r.run("tombstone: unknown key never suppressed", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            TestRunner.assertEquals(TombstoneDecision.NONE,
                    t.lookup("other", 5000, 5000), "no tombstone");
        });

        r.run("tombstone: expired after TTL -> uncertain, not suppression", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            t.addDelete("k", 2000);
            TestRunner.assertEquals(1, t.evictExpired(3001), "tombstone released past TTL");
            // upsert at 2500 with wm 3001: ttl horizon = 2001 > 2500? 3001-1000=2001, 2001>2500 false -> NONE
            TestRunner.assertEquals(TombstoneDecision.NONE,
                    t.lookup("k", 2500, 3001), "after delete but not past its own uncertainty horizon");
            // upsert at 1000: 2001 > 1000 true -> uncertain
            TestRunner.assertEquals(TombstoneDecision.UNCERTAIN,
                    t.lookup("k", 1000, 3001), "very late upsert flagged uncertain");
        });

        r.run("tombstone: retained within TTL still suppresses", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            t.addDelete("k", 2000);
            TestRunner.assertEquals(0, t.evictExpired(3000), "boundary retained");
            TestRunner.assertEquals(TombstoneDecision.SUPPRESSED,
                    t.lookup("k", 2000, 3000), "boundary still suppresses");
        });

        r.run("tombstone: capacity bound evicts oldest-delete key", () -> {
            TombstoneStore t = new TombstoneStore(2, 1_000_000);
            t.addDelete("a", 1000);
            t.addDelete("b", 2000);
            t.addDelete("c", 3000); // evicts a
            TestRunner.assertEquals(2, t.sizeKeys(), "capacity respected");
            TestRunner.assertEquals(TombstoneDecision.NONE,
                    t.lookup("a", 1000, WatermarkGenerator.NO_WATERMARK), "evicted key no longer suppresses");
            TestRunner.assertEquals(TombstoneDecision.SUPPRESSED,
                    t.lookup("c", 3000, WatermarkGenerator.NO_WATERMARK), "newest retained");
        });

        r.run("tombstone: snapshot/restore preserves delete times", () -> {
            TombstoneStore t = new TombstoneStore(10, 1000);
            t.addDelete("k", 2000);
            var snap = t.snapshot();
            TombstoneStore t2 = new TombstoneStore(10, 1000);
            t2.restore((com.example.dedup.json.Json.JsonObject) snap);
            TestRunner.assertEquals(TombstoneDecision.SUPPRESSED,
                    t2.lookup("k", 1500, WatermarkGenerator.NO_WATERMARK), "restored tombstone suppresses");
        });
    }
}
