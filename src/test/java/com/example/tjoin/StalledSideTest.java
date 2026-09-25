package com.example.tjoin;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.SideName;
import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static com.example.tjoin.TestUtil.configWithIdle;
import static com.example.tjoin.TestUtil.e;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Simulates one side stalling while the other keeps advancing.
 *
 * <p>Acceptance requirement: while a side is idle its buffered records must
 * not be discarded merely because the other side's watermark moved on, and
 * when the stalled side resumes its late-arriving-but-still-valid records
 * must still match. Idleness is decided in processing time via the
 * injectable clock/scheduler; nothing uses wall-clock threads.</p>
 */
class StalledSideTest {

    /**
     * Interval [0, 10_000], maxOutOfOrderness=0, idle timeout=5000ms of
     * processing time.
     */
    private static IntervalJoinOperator newOp(ManualClock clock) {
        return new IntervalJoinOperator(configWithIdle(0, 10_000, 0, 5_000), clock, clock);
    }

    @Test
    @DisplayName("stalled right side: left records stay buffered while right is idle")
    void rightStallKeepsLeftRecords() {
        ManualClock clock = new ManualClock(0L);
        var op = newOp(clock);

        // Right produces an early event at processing/event time 1000.
        clock.advanceTo(1000);
        op.processEvent(SideName.RIGHT, e("R-early", "k", 1000));

        // Left produces up to event time 10_000 (processing 2000).
        clock.advanceTo(2000);
        op.processEvent(SideName.LEFT, e("L1", "k", 2000));
        op.processEvent(SideName.LEFT, e("L2", "k", 10_000));

        // Right stalls. Left keeps flowing and advancing processing time far
        // past the 5000ms idle timeout.
        clock.advanceTo(8_000);
        op.processEvent(SideName.LEFT, e("L3", "k", 50_000)); // left wm -> 50_000
        clock.advanceTo(20_000);
        op.onProcessingTimeTick();
        assertTrue(op.metrics().rightIdle, "right side should be detected idle");

        // Critical guarantee: even though left watermark is 50_000, the right
        // record R-early(1000) must NOT have been cleaned: a future left
        // event at t=1000 could still match it. (Such a left event would be
        // late vs left's own wm and dropped — but right state is pruned by
        // the LEFT watermark + lowerBound; with lowerBound=0 the right
        // threshold is 50_000 and R-early < 50_000 WOULD be cleaned —
        // except the idle rule withholds the left watermark while RIGHT...
        // note which side is idle vs which state is pruned.)
        //
        // Symmetric direction: while RIGHT is idle, LEFT state cleanup
        // (driven by the RIGHT watermark) must not run, because the idle
        // side might come back with earlier timestamps. Check left state:
        assertEquals(3, op.leftBufferSize(),
                "left records must not be cleaned from the stalled right watermark");

        // Right resumes well after its idle timeout with an event that
        // matches L1 (distance 1000).
        clock.advanceTo(25_000);
        var out = op.processEvent(SideName.RIGHT, e("R-late", "k", 3000));
        assertFalse(op.metrics().rightIdle, "right side should be active again");

        assertEquals(1, out.emitted().size());
        assertEquals("L1", out.emitted().get(0).getLeftId());
        assertEquals("R-late", out.emitted().get(0).getRightId());
    }

    @Test
    @DisplayName("stalled left side: right records survive and match on recovery")
    void leftStallKeepsRightRecords() {
        ManualClock clock = new ManualClock(0L);
        var op = newOp(clock);

        clock.advanceTo(1000);
        op.processEvent(SideName.LEFT, e("L-early", "k", 1000));

        clock.advanceTo(2000);
        op.processEvent(SideName.RIGHT, e("R1", "k", 3000));
        op.processEvent(SideName.RIGHT, e("R2", "k", 9000));

        // Left stalls; right keeps moving.
        clock.advanceTo(9000);
        op.processEvent(SideName.RIGHT, e("R3", "k", 40_000)); // right wm 40_000
        clock.advanceTo(30_000);
        op.onProcessingTimeTick();
        assertTrue(op.metrics().leftIdle);

        // Right state is pruned by the LEFT watermark. Left is idle -> no
        // pruning of R1/R2 even though right wm is far ahead.
        assertEquals(3, op.rightBufferSize());

        // Left recovers; L at 8000 matches R2(9000) distance 1000 but not R1.
        clock.advanceTo(40_000);
        var out = op.processEvent(SideName.LEFT, e("L-late", "k", 8000));
        assertFalse(op.metrics().leftIdle);
        assertEquals(1, out.emitted().size());
        assertEquals("R2", out.emitted().get(0).getRightId());
    }

    @Test
    @DisplayName("idle timeout not reached: side stays active and cleanup proceeds normally")
    void notIdleBeforeTimeout() {
        ManualClock clock = new ManualClock(0L);
        var op = newOp(clock); // timeout 5000

        clock.advanceTo(1000);
        op.processEvent(SideName.RIGHT, e("R1", "k", 10_000)); // right wm 10_000

        clock.advanceTo(5000); // exactly 4000ms gap < 5000 timeout
        op.onProcessingTimeTick();
        assertFalse(op.metrics().rightIdle);

        // Boundary: 5000ms gap exactly hits the timeout.
        clock.advanceTo(6000);
        op.onProcessingTimeTick();
        assertTrue(op.metrics().rightIdle);
    }
}
