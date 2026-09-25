package com.example.dedup.tests;

import com.example.dedup.model.Event;
import com.example.dedup.model.IngestResult;
import com.example.dedup.model.WindowResult;
import com.example.dedup.pipeline.PipelineConfig;
import com.example.dedup.pipeline.StreamPipeline;
import com.example.dedup.tests.reference.RefDedup;
import com.example.dedup.tests.reference.RefTumblingWindow;
import com.example.dedup.time.ManualClock;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Differential tests: feed the same deterministic pseudo-random stream to
 * (a) the bounded pipeline and (b) exact unbounded reference
 * implementations, and check agreement everywhere the pipeline is inside
 * its promise horizon.
 */
public final class DifferentialTest {

    // No capacity eviction, generous bounds; retention covers the full
    // window lifetime (size + lateness) so window output can never fold an
    // unverifiable occurrence, and redelivery carries the SAME event time
    // as first delivery (a retry does not change the event's time).
    private static PipelineConfig cfg() {
        return new PipelineConfig(100_000, 2000, 100_000, 2000, 1000, 200, 100, 0);
    }

    public static void register(TestRunner r) {
        r.run("DIFF-1 dedup agrees with exact reference inside promise horizon", () -> {
            long seed = 42;
            for (int trial = 0; trial < 20; trial++) {
                runOne(seed + trial);
            }
        });

        r.run("DIFF-2 window results match reference for fired windows", () -> {
            var p = new StreamPipeline(cfg(), new ManualClock());
            var refWin = new RefTumblingWindow(1000);
            var refDedup = new RefDedup();
            Random rnd = new Random(7);

            int idSpace = 60;
            long[] firstTime = new long[idSpace];
            java.util.Arrays.fill(firstTime, Long.MIN_VALUE);
            long maxT = 0;
            long wm = Long.MIN_VALUE;
            for (int i = 0; i < 4000; i++) {
                int idx = rnd.nextInt(idSpace);
                String id = "e" + idx;
                long t;
                if (firstTime[idx] != Long.MIN_VALUE) {
                    t = firstTime[idx]; // pure redelivery: identical event time
                } else {
                    // bounded out-of-order, occasional forward jump
                    t = Math.max(0, maxT - rnd.nextInt(300));
                    if (rnd.nextInt(10) == 0) {
                        t = maxT + 100 + rnd.nextInt(400);
                    }
                    firstTime[idx] = t;
                    maxT = Math.max(maxT, t);
                }
                Event e = Event.upsert(id, t, "k" + rnd.nextInt(4), TestData.payload("n", rnd.nextInt(100)));

                boolean refFirst = refDedup.isFirst(id);
                IngestResult res = p.ingest(e);

                // Within promise horizon the pipeline verdict must equal ground truth.
                boolean inPromise = wm == Long.MIN_VALUE || wm - 2000 <= t;
                if (inPromise && !res.windowLateDropped() && !res.suppressedByTombstone()) {
                    TestRunner.assertEquals(refFirst, !res.duplicate() && res.accepted(),
                            "trial t=" + t + " id=" + id);
                }
                if (res.accepted() && !res.suppressedByTombstone() && !res.windowLateDropped()) {
                    refWin.add(e);
                }
                // periodically push watermark, never enough to expire live skewed ids
                wm = maxT - 100;
                p.advanceWatermark(wm);
            }

            // Compare every window the pipeline emitted against the reference.
            List<WindowResult> got = p.drainWindowOutputs();
            // fire everything remaining up to a horizon the pipeline actually reached
            long horizon = p.watermark();
            p.advanceWatermark(horizon + 100_000);
            got.addAll(p.drainWindowOutputs());

            List<WindowResult> expected = refWin.resultsUpTo(Long.MAX_VALUE);
            // Compare as multisets of (start,key,upserts,deletes,lastId)
            TestRunner.assertEquals(expected.size(), got.size(),
                    "window result count (expected " + expected.size() + " got " + got.size() + ")");
            for (int i = 0; i < expected.size(); i++) {
                WindowResult a = expected.get(i);
                WindowResult b = got.get(i);
                TestRunner.assertEquals(a.windowStart(), b.windowStart(), "window start " + i);
                TestRunner.assertEquals(a.key(), b.key(), "key " + i);
                TestRunner.assertEquals(a.upsertCount(), b.upsertCount(), "upserts " + i);
                TestRunner.assertEquals(a.deleteCount(), b.deleteCount(), "deletes " + i);
                TestRunner.assertEquals(a.lastUpsertId(), b.lastUpsertId(), "last upsert id " + i);
            }
        });
    }

    private static void runOne(long seed) {
        var p = new StreamPipeline(cfg(), new ManualClock());
        var ref = new RefDedup();
        Random rnd = new Random(seed);

        int idSpace = 40;
        long[] firstTime = new long[idSpace];
        java.util.Arrays.fill(firstTime, Long.MIN_VALUE);
        long maxT = 0;
        long wm = Long.MIN_VALUE;
        List<String> violations = new ArrayList<>();

        for (int i = 0; i < 2000; i++) {
            int idx = rnd.nextInt(idSpace);
            String id = "id" + idx;
            long t;
            if (firstTime[idx] != Long.MIN_VALUE && rnd.nextInt(3) != 0) {
                // redelivery: exact same event time as first delivery
                t = firstTime[idx];
            } else if (firstTime[idx] == Long.MIN_VALUE) {
                t = Math.max(0, maxT - rnd.nextInt(250));
                if (rnd.nextInt(8) == 0) {
                    t = maxT + rnd.nextInt(300);
                }
                firstTime[idx] = t;
                maxT = Math.max(maxT, t);
            } else {
                t = Math.max(0, maxT - rnd.nextInt(250));
                maxT = Math.max(maxT, t);
            }
            Event e = Event.upsert(id, t, "k", TestData.payload("n", i));
            boolean refFirst = ref.isFirst(id);
            IngestResult res = p.ingest(e);

            boolean inPromise = wm == Long.MIN_VALUE || wm - 2000 <= t;
            if (inPromise && !res.windowLateDropped()) {
                if (refFirst != (!res.duplicate() && res.accepted())) {
                    violations.add("t=" + t + " id=" + id + " refFirst=" + refFirst
                            + " pipelineDup=" + res.duplicate() + " accepted=" + res.accepted());
                }
            }
            wm = maxT - 100;
            p.advanceWatermark(wm);
        }
        TestRunner.assertTrue(violations.isEmpty(),
                "seed " + seed + " disagreements: " + violations.stream().limit(5).toList());
    }
}
