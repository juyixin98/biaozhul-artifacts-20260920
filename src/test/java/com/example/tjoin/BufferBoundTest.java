package com.example.tjoin;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.AcceptStatus;
import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.SideName;
import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static com.example.tjoin.TestUtil.configWithCap;
import static com.example.tjoin.TestUtil.e;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

/**
 * Buffer bounding: REJECT must not discard already-buffered matchable
 * records; DROP_OLDEST evicts the oldest and accounts for it. Also covers
 * watermark-driven cleanup relieving the cap.
 */
class BufferBoundTest {

    @Test
    @DisplayName("REJECT: over-cap event refused, existing buffered records intact and still match")
    void rejectKeepsBufferedRecords() {
        ManualClock c = new ManualClock(0L);
        var op = new IntervalJoinOperator(
                configWithCap(0, 1000, 2, 2, BufferOverflowPolicy.REJECT), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 1000));
        op.processEvent(SideName.LEFT, e("L2", "k", 2000));

        var third = op.processEvent(SideName.LEFT, e("L3", "k", 3000));
        assertEquals(AcceptStatus.BUFFER_FULL, third.status());
        assertEquals(1, op.metrics().bufferRejected.get());
        assertEquals(2, op.leftBufferSize());

        // The rejected event id was never admitted; redelivery of it is a
        // fresh attempt, not a DUPLICATE.
        var retry = op.processEvent(SideName.LEFT, e("L3", "k", 3000));
        assertEquals(AcceptStatus.BUFFER_FULL, retry.status());

        // Buffered records remain fully matchable.
        var out = op.processEvent(SideName.RIGHT, e("R1", "k", 1000));
        assertEquals(1, out.emitted().size());
        assertEquals("L1", out.emitted().get(0).getLeftId());
    }

    @Test
    @DisplayName("DROP_OLDEST: oldest event evicted to make room")
    void dropOldestEvicts() {
        ManualClock c = new ManualClock(0L);
        var op = new IntervalJoinOperator(
                configWithCap(0, 1000, 2, 2, BufferOverflowPolicy.DROP_OLDEST), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 1000));
        op.processEvent(SideName.LEFT, e("L2", "k", 2000));

        var third = op.processEvent(SideName.LEFT, e("L3", "k", 3000));
        assertEquals(AcceptStatus.ACCEPTED, third.status());
        assertNotNull(third.evicted());
        assertEquals("L1", third.evicted().getId());
        assertEquals(1, op.metrics().oldestEvicted.get());
        assertEquals(2, op.leftBufferSize());
    }

    @Test
    @DisplayName("cap is enforced after watermark cleanup frees space")
    void cleanupRelievesCap() {
        ManualClock c = new ManualClock(0L);
        // narrow interval [0,0] makes aggressive cleanup; cap 2 on left.
        var op = new IntervalJoinOperator(
                configWithCap(0, 0, 2, 2, BufferOverflowPolicy.REJECT), c, c);

        op.processEvent(SideName.LEFT, e("L1", "k", 1000));
        op.processEvent(SideName.LEFT, e("L2", "k", 2000));

        // A right event at 5000 on a DIFFERENT key drives right wm to 5000;
        // left threshold = 5000 - 0 = 5000: L1,L2 (t<5000) get cleaned.
        op.processEvent(SideName.RIGHT, e("Rx", "other", 5000));
        assertEquals(0, op.leftBufferSize());

        // Room is available again.
        var admitted = op.processEvent(SideName.LEFT, e("L3", "k", 5000));
        assertEquals(AcceptStatus.ACCEPTED, admitted.status());
        assertEquals(0, op.metrics().bufferRejected.get());
    }

    @Test
    @DisplayName("DROP_OLDEST eviction is global across keys (oldest event time wins)")
    void dropOldestGlobalAcrossKeys() {
        ManualClock c = new ManualClock(0L);
        var op = new IntervalJoinOperator(
                configWithCap(0, 100_000, 3, 3, BufferOverflowPolicy.DROP_OLDEST), c, c);

        op.processEvent(SideName.LEFT, e("L1", "a", 1000));
        op.processEvent(SideName.LEFT, e("L2", "b", 2000));
        op.processEvent(SideName.LEFT, e("L3", "a", 3000));

        var fourth = op.processEvent(SideName.LEFT, e("L4", "b", 4000));
        assertEquals("L1", fourth.evicted().getId());

        // Right at t=4000,key a: L1(a,1000) was evicted; L3(a,3000) has
        // distance 1000 (within [0,100000]) and alone matches. L2/L4 key b.
        var match = op.processEvent(SideName.RIGHT, e("R1", "a", 4000));
        assertEquals(1, match.emitted().size(), "L3 still matches; evicted L1 does not");
        assertEquals("L3", match.emitted().get(0).getLeftId());
    }
}
