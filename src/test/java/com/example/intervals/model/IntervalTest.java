package com.example.intervals.model;

import com.example.intervals.algebra.IntervalAlgebra;
import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalTest {

    @Test
    void openClosedEndpointMembership() {
        Interval<Integer> closedOpen = Interval.closedOpen(1, 3); // [1,3)
        assertTrue(closedOpen.contains(1));
        assertTrue(closedOpen.contains(2));
        assertFalse(closedOpen.contains(3));

        Interval<Integer> openClosed = Interval.openClosed(1, 3); // (1,3]
        assertFalse(openClosed.contains(1));
        assertTrue(openClosed.contains(2));
        assertTrue(openClosed.contains(3));

        Interval<Integer> open = Interval.open(1, 3); // (1,3)
        assertFalse(open.contains(1));
        assertFalse(open.contains(3));
        assertTrue(open.contains(2));
    }

    @Test
    void singletonIntervalIsExactlyOnePoint() {
        Interval<Integer> singleton = Interval.closed(2, 2); // [2,2]
        assertFalse(singleton.isEmpty());
        assertTrue(singleton.contains(2));
        assertFalse(singleton.contains(1));
        assertFalse(singleton.contains(3));
    }

    @Test
    void degenerateEqualValueOpenFormsAreEmpty() {
        assertTrue(Interval.open(2, 2).isEmpty());     // (2,2)
        assertTrue(Interval.closedOpen(2, 2).isEmpty()); // [2,2)
        assertTrue(Interval.openClosed(2, 2).isEmpty()); // (2,2]
    }

    @Test
    void unboundedIntervalsContainWithoutBound() {
        Interval<Integer> all = Interval.all();
        assertTrue(all.contains(Integer.MIN_VALUE));
        assertTrue(all.contains(Integer.MAX_VALUE));

        Interval<Integer> leftHalf = Interval.between(null, false, 5, true); // (-inf,5)
        assertTrue(leftHalf.contains(-1_000_000));
        assertFalse(leftHalf.contains(5));

        Interval<Integer> rightHalf = Interval.between(5, false, null, false); // [5,+inf)
        assertTrue(rightHalf.contains(5));
        assertTrue(rightHalf.contains(1_000_000));
    }

    @Test
    void reversedFiniteIntervalsAreRejected() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> Interval.closed(5, 2));
        assertEquals(ErrorCode.REVERSED_INTERVAL, e.errorCode());

        assertThrows(IntervalException.class, () -> Interval.open(3, 1));
        assertThrows(IntervalException.class, () -> Interval.closedOpen(9, 9 - 1));
    }

    @Test
    void infiniteEndsAreNeverReversed() {
        // null endpoint = infinity; these are always well-formed.
        Interval.between(null, true, 0, true);
        Interval.between(0, false, null, false);
        Interval.<Integer>all();
    }

    @Test
    void adjacentOpenAndClosedRangesNormalizeTogether() {
        // [1,2] U (2,3] covers every point 1,2,3 with no gap and must become [1,3].
        IntervalSet<Integer> merged = new IntervalAlgebra<Integer>()
                .normalize(List.of(Interval.closed(1, 2), Interval.openClosed(2, 3)));
        assertEquals(List.of(Interval.closed(1, 3)), merged.intervals());
    }

    @Test
    void pointGapKeepsIntervalsSeparate() {
        // [1,2] U (2,3] excludes nothing between 1 and 3 except no point ... it is [1,3].
        IntervalSet<Integer> merged = new IntervalAlgebra<Integer>()
                .normalize(List.of(Interval.closed(1, 2), Interval.openClosed(2, 3)));
        assertEquals(1, merged.intervals().size());

        // But excluding a single point x splits into [...,x) U (x,...].
        IntervalSet<Integer> split = new IntervalAlgebra<Integer>()
                .normalize(List.of(Interval.closedOpen(1, 2), Interval.openClosed(2, 3)));
        assertEquals(2, split.intervals().size());
        assertEquals(Cut.below(1), split.intervals().get(0).lowerCut());
        assertEquals(Cut.below(2), split.intervals().get(0).upperCut());
        assertEquals(Cut.above(2), split.intervals().get(1).lowerCut());
        assertEquals(Cut.above(3), split.intervals().get(1).upperCut());
    }
}
