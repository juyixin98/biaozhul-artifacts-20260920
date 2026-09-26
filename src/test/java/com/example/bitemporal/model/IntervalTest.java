package com.example.bitemporal.model;

import org.junit.jupiter.api.Test;

import java.time.LocalDate;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalTest {

    private static LocalDate d(int month, int day) {
        return LocalDate.of(2025, month, day);
    }

    @Test
    void halfOpenContainsStartButNotEnd() {
        Interval i = Interval.of(d(1, 1), d(2, 1));
        assertTrue(i.contains(d(1, 1)));   // 起点包含
        assertTrue(i.contains(d(1, 31)));
        assertFalse(i.contains(d(2, 1)));  // 终点不包含
        assertFalse(i.contains(d(2, 2)));
    }

    @Test
    void openEndedContainsEveryLaterDate() {
        Interval i = Interval.openEnded(d(3, 1));
        assertTrue(i.contains(d(3, 1)));
        assertTrue(i.contains(LocalDate.of(2099, 12, 31)));
        assertNull(i.to());
    }

    @Test
    void abuttingIntervalsDoNotOverlap() {
        // 半开：[01-01,03-01) 与 [03-01,05-01) 仅端点相接，不算重叠
        Interval a = Interval.of(d(1, 1), d(3, 1));
        Interval b = Interval.of(d(3, 1), d(5, 1));
        assertFalse(a.overlaps(b));
        assertFalse(b.overlaps(a));
        assertNull(a.intersection(b));
    }

    @Test
    void overlappingByOneDayIntersects() {
        Interval a = Interval.of(d(1, 1), d(3, 1));
        Interval b = Interval.of(d(2, 28), d(5, 1));
        assertTrue(a.overlaps(b));
        assertEquals(Interval.of(d(2, 28), d(3, 1)), a.intersection(b));
    }

    @Test
    void openEndedOverlapRules() {
        Interval open = Interval.openEnded(d(6, 1));
        assertTrue(open.overlaps(Interval.of(d(5, 1), d(6, 2))));
        assertFalse(open.overlaps(Interval.of(d(5, 1), d(6, 1)))); // 相接
        assertTrue(open.overlaps(Interval.openEnded(d(12, 1))));
    }

    @Test
    void rejectsEmptyOrInvertedIntervals() {
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(d(1, 1), d(1, 1)));
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(d(2, 1), d(1, 1)));
    }
}
