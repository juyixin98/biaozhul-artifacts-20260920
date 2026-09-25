package com.example.tjoin;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.SideName;
import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static com.example.tjoin.TestUtil.config;
import static com.example.tjoin.TestUtil.e;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Independent per-side watermarks and state cleanup behavior, including:
 * <ul>
 *   <li>cleanup only removes records that can no longer match,</li>
 *   <li>watermark boundary (cleaned records are exactly the unmatchable),</li>
 *   <li>late events behind the own-side watermark are dropped,</li>
 *   <li>each side's watermark drives only the opposite side's threshold.</li>
 * </ul>
 */
class WatermarkCleanupTest {

    private static ManualClock clock() {
        return new ManualClock(0L);
    }

    @Test
    @DisplayName("right watermark advance cleans only left records beyond reach; matchable records stay")
    void cleanupKeepsMatchable() {
        // tR - tL in [0, 5000]
        ManualClock c = clock();
        var op = new IntervalJoinOperator(config(0, 5000), c, c);

        op.processEvent(SideName.LEFT, e("L-far", "k", 1000));
        op.processEvent(SideName.LEFT, e("L-edge", "k", 5000));
        op.processEvent(SideName.LEFT, e("L-near", "k", 9000));

        // Right events at t=10_000: right watermark becomes 10_000.
        // Left records with tL < 10_000 - 5000 = 5000 are cleaned strictly:
        //   L-far(1000) gone; L-edge(5000) and L-near(9000) stay (a right
        //   at t=10_000 pairs with L-edge at distance exactly 5000).
        op.processEvent(SideName.RIGHT, e("R1", "other", 10_000));

        assertEquals(1, op.metrics().leftStateCleaned.get());
        assertEquals(2, op.leftBufferSize());

        // Both retained left events match a right event at 10_000.
        var out = op.processEvent(SideName.RIGHT, e("R2", "k", 10_000));
        assertEquals(2, out.emitted().size());
    }

    @Test
    @DisplayName("left watermark cleans right records below lowerBound reach")
    void cleanupRightSide() {
        // distance in [-2000, 5000]
        ManualClock c = clock();
        var op = new IntervalJoinOperator(config(-2000, 5000), c, c);

        op.processEvent(SideName.RIGHT, e("R-old", "k", 7000));
        op.processEvent(SideName.RIGHT, e("R-edge", "k", 8000));
        op.processEvent(SideName.RIGHT, e("R-keep", "k", 9000));

        // Left event at 10_000 -> left watermark 10_000.
        // Right records tR < 10_000 + (-2000) = 8000 cleaned strictly.
        op.processEvent(SideName.LEFT, e("L1", "other", 10_000));

        assertEquals(1, op.metrics().rightStateCleaned.get()); // R-old(7000)
        assertEquals(2, op.rightBufferSize());

        // A left at 10_000 matches R-edge(8000) d=-2000 and R-keep(9000).
        var out = op.processEvent(SideName.LEFT, e("L2", "k", 10_000));
        assertEquals(2, out.emitted().size());
    }

    @Test
    @DisplayName("explicit watermark injection triggers equivalent cleanup")
    void explicitWatermark() {
        ManualClock c = clock();
        var op = new IntervalJoinOperator(config(0, 1000), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 1000));
        op.processEvent(SideName.LEFT, e("L2", "k", 2000));
        op.processEvent(SideName.LEFT, e("L3", "k", 3000));

        // Advance right watermark to 3500 without any right event.
        // Threshold for left: 3500 - 1000 = 2500; remove tL < 2500.
        int cleaned = op.advanceWatermark(SideName.RIGHT, 3500);
        assertEquals(2, cleaned);
        assertEquals(1, op.leftBufferSize());
    }

    @Test
    @DisplayName("event behind its OWN side watermark is dropped as late")
    void lateEventDropped() {
        ManualClock c = clock();
        var op = new IntervalJoinOperator(config(0, 5000), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 10_000)); // left wm = 10_000

        var late = op.processEvent(SideName.LEFT, e("L-late", "k", 9999));
        assertEquals(com.example.tjoin.model.AcceptStatus.LATE, late.status());
        assertEquals(1, op.metrics().lateDropped.get());

        // Event exactly at watermark is NOT late (strictly-behind rule).
        var edge = op.processEvent(SideName.LEFT, e("L-edge", "k", 10_000));
        assertEquals(com.example.tjoin.model.AcceptStatus.ACCEPTED, edge.status());
    }

    @Test
    @DisplayName("bounded out-of-orderness: watermark lags behind max event time")
    void outOfOrdernessLag() {
        ManualClock c = clock();
        var op = new IntervalJoinOperator(
                TestUtil.config(0, 5000, 2000, 2000), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 10_000)); // wm = 8000
        assertEquals(8000, op.leftWatermark());

        // t=9000 is not behind wm=8000 -> admitted (out-of-order tolerated).
        var reordered = op.processEvent(SideName.LEFT, e("L2", "k", 9000));
        assertEquals(com.example.tjoin.model.AcceptStatus.ACCEPTED, reordered.status());

        // t=7999 is behind wm=8000 -> late.
        var tooLate = op.processEvent(SideName.LEFT, e("L3", "k", 7999));
        assertEquals(com.example.tjoin.model.AcceptStatus.LATE, tooLate.status());
    }

    @Test
    @DisplayName("independent watermarks: advancing right never cleans right state; left wm stays MIN")
    void independentWatermarks() {
        ManualClock c = clock();
        var op = new IntervalJoinOperator(config(0, 5000), c, c);

        op.processEvent(SideName.RIGHT, e("R1", "k", 100_000));
        // Left has never seen an event/watermark.
        assertEquals(Long.MIN_VALUE, op.leftWatermark());
        // Right watermark must not clean right state itself:
        // right t=100_000 can still match a future left t in [95_000,100_000].
        assertEquals(1, op.rightBufferSize());
        assertEquals(0, op.metrics().rightStateCleaned.get());
        assertTrue(op.leftBufferSize() == 0);
    }
}
