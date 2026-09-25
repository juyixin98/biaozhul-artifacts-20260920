package com.example.dedup.tests;

import com.example.dedup.watermark.BoundedOutOfOrdernessWatermarks;
import com.example.dedup.watermark.WatermarkGenerator;

/** Watermark heuristic and explicit-advance semantics. */
public final class WatermarkTest {

    public static void register(TestRunner r) {
        r.run("wm: NO_WATERMARK before observations", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(100);
            TestRunner.assertEquals(WatermarkGenerator.NO_WATERMARK, w.watermark(), "none");
        });

        r.run("wm: max observed minus slack", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(100);
            w.observe(1000);
            TestRunner.assertEquals(900L, w.watermark(), "900");
            w.observe(1200);
            TestRunner.assertEquals(1100L, w.watermark(), "1100");
        });

        r.run("wm: out-of-order (older) observation never moves it back", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(100);
            w.observe(2000);
            TestRunner.assertEquals(1900L, w.watermark(), "1900");
            w.observe(500);
            TestRunner.assertEquals(1900L, w.watermark(), "held");
        });

        r.run("wm: zero slack gives exact max", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(0);
            w.observe(1000);
            TestRunner.assertEquals(1000L, w.watermark(), "exact");
        });

        r.run("wm: explicit advance moves forward; regression rejected", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(100);
            w.observe(1000); // 900
            TestRunner.assertTrue(w.tryAdvance(5000), "advance to 5000");
            TestRunner.assertEquals(5000L, w.watermark(), "explicit wins over heuristic");
            TestRunner.assertFalse(w.tryAdvance(4000), "cannot move back");
            TestRunner.assertFalse(w.tryAdvance(5000), "equal is also rejected");
            TestRunner.assertEquals(5000L, w.watermark(), "unchanged");
        });

        r.run("wm: later heuristic observation cannot undercut explicit floor", () -> {
            var w = new BoundedOutOfOrdernessWatermarks(100);
            w.tryAdvance(5000);
            w.observe(2000);
            TestRunner.assertEquals(5000L, w.watermark(), "floor kept");
        });
    }
}
