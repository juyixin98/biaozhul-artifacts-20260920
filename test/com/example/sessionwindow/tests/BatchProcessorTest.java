package com.example.sessionwindow.tests;

import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;
import com.example.sessionwindow.service.BatchProcessor;

import java.util.List;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertFalse;
import static com.example.sessionwindow.tests.Assert.assertTrue;

public class BatchProcessorTest {

    private static BatchProcessor.Item ev(String key, long ts) {
        return BatchProcessor.Item.event(Event.of(key, ts));
    }

    @Test
    void acceptanceScenarioMatchesReferenceAndCleansState() {
        // Out-of-order, gap=10, allowedLateness=20.
        List<BatchProcessor.Item> items = List.of(
                ev("u1", 0),
                ev("u1", 15),
                ev("u2", 100),
                BatchProcessor.Item.watermark(25),   // seals u1's two sessions
                ev("u1", 10),                        // late bridge (t=10 touches both)
                BatchProcessor.Item.watermark(110),  // seals u2; drop threshold = 90
                ev("u1", 80),                        // 80 < 110-20=90 -> dropped
                BatchProcessor.Item.watermark(200)
        );
        BatchProcessor.Outcome out = BatchProcessor.run(10, 20, items, true);

        assertTrue(out.matchesReference, "materialized result equals offline grouping");
        assertEquals(0, out.remainingSessions, "all state purged after flush");
        assertEquals(0, out.remainingKeys, "no keys retained after flush");

        List<ResultRecord> records = out.records;
        long retracts = records.stream().filter(r -> r.type() == ResultRecord.Type.RETRACT).count();
        assertTrue(retracts >= 2, "bridge produced >=2 retractions, got " + retracts);
        assertEquals(1, out.droppedEvents.size(), "exactly one event dropped");
        assertEquals(80L, out.droppedEvents.get(0).timestamp(), "dropped t=80");

        // Reference is computed only from accepted events.
        assertEquals(1, out.reference.get("u1").size(), "u1 bridged to one offline session");
        assertEquals(0L, out.reference.get("u1").get(0).start(), "u1 session start 0");
        assertEquals(25L, out.reference.get("u1").get(0).end(), "u1 session end 25");
        assertEquals(3L, out.reference.get("u1").get(0).count(), "u1 three accepted events");
    }

    @Test
    void withoutFlushOpenSessionsAreRetained() {
        List<BatchProcessor.Item> items = List.of(ev("u1", 1), ev("u1", 2));
        BatchProcessor.Outcome out = BatchProcessor.run(5, 0, items, false);
        assertEquals(1, out.remainingSessions, "open session retained without flush");
    }

    @Test
    void droppedEventsAreExcludedFromReferenceInput() {
        List<BatchProcessor.Item> items = List.of(
                ev("u1", 0),
                BatchProcessor.Item.watermark(100),
                ev("u1", 10) // late beyond any reasonable lateness
        );
        BatchProcessor.Outcome out = BatchProcessor.run(5, 0, items, true);
        // Only t=0 accepted -> reference single session [0,5], online same.
        assertTrue(out.matchesReference, "still matches (reference built from accepted only)");
        assertEquals(1, out.droppedEvents.size(), "t=10 dropped");
        assertEquals(1, out.acceptedEvents.size(), "only t=0 accepted");
    }

    @Test
    void zeroAllowedLatenessSealsAndPurgesTogether() {
        List<BatchProcessor.Item> items = List.of(
                ev("u1", 0),
                BatchProcessor.Item.watermark(10)
        );
        BatchProcessor.Outcome out = BatchProcessor.run(10, 0, items, false);
        assertEquals(0, out.remainingSessions, "purged immediately at wm=end with zero lateness");
        assertFalse(out.records.isEmpty(), "records emitted");
    }
}
