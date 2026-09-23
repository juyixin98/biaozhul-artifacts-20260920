package com.example.sessionwindow.tests;

import com.example.sessionwindow.engine.ReferenceGrouper;
import com.example.sessionwindow.engine.Results;
import com.example.sessionwindow.engine.SessionWindowEngine;
import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.Event;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.TreeMap;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertTrue;

/**
 * Property test: feed random out-of-order streams with advancing watermarks
 * and a large allowedLateness (so nothing is dropped), then flush and compare
 * the folded online result against the exact offline grouping.
 */
public class ReferenceEquivalenceTest {

    private static final long GAP = 10;

    @Test
    void randomOutOfOrderStreamsMatchOfflineReference() {
        Random rnd = new Random(20260923L);
        for (int trial = 0; trial < 300; trial++) {
            runOneTrial(rnd, trial);
        }
    }

    @Test
    void randomStreamWithLateBridgesMatchesReference() {
        Random rnd = new Random(42L);
        for (int trial = 0; trial < 100; trial++) {
            // Sparse timestamps so sessions genuinely split and later bridge.
            runOneTrial(rnd, trial, 25, 6, 40, 60);
        }
    }

    private void runOneTrial(Random rnd, int trial) {
        runOneTrial(rnd, trial, 15, 4, 12, 1000);
    }

    private void runOneTrial(Random rnd, int trial, int eventCount, int keyCount,
                             int timeRange, long allowedLateness) {
        List<Event> all = new ArrayList<>();
        List<Event> shuffled = new ArrayList<>();
        for (int i = 0; i < eventCount; i++) {
            String key = "k" + rnd.nextInt(keyCount);
            long ts = rnd.nextInt(timeRange);
            double value = Math.round((rnd.nextDouble() * 10) * 100) / 100.0;
            Event e = Event.of(key, ts, value);
            all.add(e);
            shuffled.add(e);
        }
        Collections.shuffle(shuffled, rnd);

        SessionWindowEngine engine = new SessionWindowEngine(GAP, allowedLateness);

        // Interleave watermarks between events based on shuffled-order max ts.
        long maxTs = Long.MIN_VALUE;
        int idx = 0;
        for (Event e : shuffled) {
            engine.processEvent(e);
            maxTs = Math.max(maxTs, e.timestamp());
            if (idx % 4 == 3) {
                // watermark slightly behind the observed max, never dropping
                // events within this stream's timestamps
                long wm = Math.max(0, maxTs - rnd.nextInt(3));
                engine.processWatermark(wm);
            }
            idx++;
        }
        engine.flush();

        Map<String, TreeMap<Long, Aggregate>> online = Results.byKey(engine.records());
        Map<String, List<Aggregate>> offline = ReferenceGrouper.group(all, GAP);

        assertEquals(offline.keySet(), online.keySet(),
                "trial " + trial + ": key sets differ");
        for (String key : offline.keySet()) {
            List<Aggregate> got = new ArrayList<>(online.get(key).values());
            List<Aggregate> want = offline.get(key);
            assertEquals(want.size(), got.size(),
                    "trial " + trial + " key " + key + ": session count");
            for (int i = 0; i < want.size(); i++) {
                assertAggregateEqual(want.get(i), got.get(i),
                        "trial " + trial + " key " + key + " session " + i);
            }
        }
        assertEquals(0, engine.totalSessionCount(), "trial " + trial + ": state cleaned after flush");
        assertTrue(engine.records().stream().noneMatch(r -> r.type() == com.example.sessionwindow.model.ResultRecord.Type.DROPPED),
                "trial " + trial + ": nothing dropped with huge lateness");
    }

    static void assertAggregateEqual(Aggregate want, Aggregate got, String message) {
        assertEquals(want.start(), got.start(), message + " start");
        assertEquals(want.end(), got.end(), message + " end");
        assertEquals(want.count(), got.count(), message + " count");
        assertEquals(want.sum(), got.sum(), 1e-6, message + " sum");
        assertEquals(want.min(), got.min(), 1e-6, message + " min");
        assertEquals(want.max(), got.max(), 1e-6, message + " max");
    }
}
