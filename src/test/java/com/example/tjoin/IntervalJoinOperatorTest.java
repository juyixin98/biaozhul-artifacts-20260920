package com.example.tjoin;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.AcceptStatus;
import com.example.tjoin.model.JoinResult;
import com.example.tjoin.model.ProcessResult;
import com.example.tjoin.model.SideName;
import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.util.List;

import static com.example.tjoin.TestUtil.config;
import static com.example.tjoin.TestUtil.e;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Basic interval semantics, both arrival orders, time boundaries and
 * duplicate-value handling.
 */
class IntervalJoinOperatorTest {

    /** Fresh operator plus its manual clock (time starts at 1000). */
    private static IntervalJoinOperator op(com.example.tjoin.model.JoinConfig cfg, ManualClock clock) {
        return new IntervalJoinOperator(cfg, clock, clock);
    }

    private static ManualClock clock() {
        return new ManualClock(1000L);
    }

    private static List<JoinResult> push(IntervalJoinOperator op, SideName side,
                                         com.example.tjoin.model.StreamEvent ev) {
        return op.processEvent(side, ev).emitted();
    }

    @Test
    @DisplayName("left then right, within interval: pair emitted when right arrives")
    void leftThenRightMatch() {
        ManualClock c = clock();
        var op = op(config(-2000, 5000), c);

        assertTrue(push(op, SideName.LEFT, e("L1", "k1", 10_000)).isEmpty());
        var out = push(op, SideName.RIGHT, e("R1", "k1", 12_000));

        assertEquals(1, out.size());
        assertEquals("L1", out.get(0).getLeftId());
        assertEquals("R1", out.get(0).getRightId());
        assertEquals(2000, out.get(0).getTimeDistance());
        assertEquals(1, op.metrics().pairsEmitted.get());
    }

    @Test
    @DisplayName("right then left (negative distance) matches too")
    void rightThenLeftMatch() {
        ManualClock c = clock();
        var op = op(config(-2000, 5000), c);

        assertTrue(push(op, SideName.RIGHT, e("R1", "k1", 9_000)).isEmpty());
        // tR - tL = -1000 >= lowerBound(-2000)
        var out = push(op, SideName.LEFT, e("L1", "k1", 10_000));

        assertEquals(1, out.size());
        assertEquals(1, op.metrics().pairsEmitted.get());
    }

    @Test
    @DisplayName("different keys never match")
    void differentKeys() {
        ManualClock c = clock();
        var op = op(config(-2000, 5000), c);

        push(op, SideName.LEFT, e("L1", "orders", 10_000));
        var out = push(op, SideName.RIGHT, e("R1", "payments", 10_000));

        assertTrue(out.isEmpty());
        assertEquals(0, op.metrics().pairsEmitted.get());
    }

    @Nested
    @DisplayName("inclusive time boundaries")
    class Boundaries {

        @Test
        @DisplayName("distance == lowerBound matches")
        void atLowerBound() {
            ManualClock c = clock();
            var op = op(config(-2000, 5000), c);
            push(op, SideName.LEFT, e("L1", "k", 12_000));
            assertEquals(1, push(op, SideName.RIGHT, e("R1", "k", 10_000)).size());
        }

        @Test
        @DisplayName("distance == upperBound matches")
        void atUpperBound() {
            ManualClock c = clock();
            var op = op(config(-2000, 5000), c);
            push(op, SideName.LEFT, e("L1", "k", 10_000));
            assertEquals(1, push(op, SideName.RIGHT, e("R1", "k", 15_000)).size());
        }

        @Test
        @DisplayName("distance == lowerBound - 1 does not match")
        void justBelowLowerBound() {
            ManualClock c = clock();
            var op = op(config(-2000, 5000), c);
            push(op, SideName.LEFT, e("L1", "k", 12_001));
            assertEquals(0, push(op, SideName.RIGHT, e("R1", "k", 10_000)).size());
        }

        @Test
        @DisplayName("distance == upperBound + 1 does not match")
        void justAboveUpperBound() {
            ManualClock c = clock();
            var op = op(config(-2000, 5000), c);
            push(op, SideName.LEFT, e("L1", "k", 10_000));
            assertEquals(0, push(op, SideName.RIGHT, e("R1", "k", 15_001)).size());
        }

        @Test
        @DisplayName("zero-width interval [0,0] matches same-timestamp events")
        void zeroWidthInterval() {
            ManualClock c = clock();
            var op = op(config(0, 0), c);
            push(op, SideName.LEFT, e("L1", "k", 10_000));
            assertEquals(1, push(op, SideName.RIGHT, e("R1", "k", 10_000)).size());
            assertEquals(0, push(op, SideName.RIGHT, e("R2", "k", 10_001)).size());
        }
    }

    @Test
    @DisplayName("duplicate VALUES are allowed: two identical events each match the opposite record")
    void duplicateValuesAllowed() {
        ManualClock c = clock();
        var op = op(config(-2000, 5000), c);

        // Two right events with identical key/time/value but distinct ids.
        push(op, SideName.RIGHT, e("R1", "k1", 11_000, "v"));
        push(op, SideName.RIGHT, e("R2", "k1", 11_000, "v"));
        var out = push(op, SideName.LEFT, e("L1", "k1", 10_000, "v"));

        assertEquals(2, out.size());
        assertEquals(2, op.metrics().pairsEmitted.get());
    }

    @Test
    @DisplayName("duplicate event ID redelivery is ignored (per side)")
    void duplicateIdIgnored() {
        ManualClock c = clock();
        var op = op(config(-2000, 5000), c);

        ProcessResult first = op.processEvent(SideName.LEFT, e("L1", "k1", 10_000));
        ProcessResult second = op.processEvent(SideName.LEFT, e("L1", "k1", 10_000));

        assertEquals(AcceptStatus.ACCEPTED, first.status());
        assertEquals(AcceptStatus.DUPLICATE, second.status());
        assertEquals(1, op.metrics().duplicatesDropped.get());

        // Same id reused on the OTHER side is a different record.
        ProcessResult otherSide = op.processEvent(SideName.RIGHT, e("L1", "k1", 10_000));
        assertEquals(AcceptStatus.ACCEPTED, otherSide.status());
    }

    @Test
    @DisplayName("each pair emitted exactly once; a later event never re-emits an old pair")
    void pairEmittedOnce() {
        // Per-side event times must be non-decreasing (ooo=0) to avoid late drops.
        ManualClock c = clock();
        var op = op(config(-10_000, 10_000), c);

        push(op, SideName.LEFT, e("L1", "k", 10_000));
        push(op, SideName.RIGHT, e("R1", "k", 11_000));   // emits (L1,R1)
        push(op, SideName.LEFT, e("L2", "k", 11_500));    // emits (L2,R1), d=-500
        push(op, SideName.RIGHT, e("R2", "k", 12_000));   // emits (L1,R2),(L2,R2)

        // All four distinct pairs emitted, no double emission.
        assertEquals(4, op.metrics().pairsEmitted.get());
        assertEquals(0, op.metrics().pairsSuppressed.get());
    }

    @Test
    @DisplayName("one event matches many opposite events (fan-out)")
    void fanOut() {
        ManualClock c = clock();
        var op = op(config(0, 1000), c);
        push(op, SideName.RIGHT, e("R1", "k", 10_300));
        push(op, SideName.RIGHT, e("R2", "k", 10_700));
        push(op, SideName.RIGHT, e("R3", "k", 11_000));
        push(op, SideName.RIGHT, e("R4", "k", 11_001)); // out

        var out = push(op, SideName.LEFT, e("L1", "k", 10_000));
        assertEquals(List.of("R1", "R2", "R3"),
                out.stream().map(JoinResult::getRightId).toList());
    }
}
