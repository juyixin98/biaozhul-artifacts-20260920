package bitemporal.model;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.Instant;

import org.junit.jupiter.api.Test;

class IntervalTest {

    private static final Instant T0 = Instant.parse("2026-01-01T00:00:00Z");
    private static final Instant T1 = Instant.parse("2026-04-01T00:00:00Z");
    private static final Instant T2 = Instant.parse("2026-07-01T00:00:00Z");

    @Test
    void containsUsesHalfOpenBoundaries() {
        Interval q1 = Interval.of(T0, T1);

        assertTrue(q1.contains(T0), "from endpoint is included");
        assertTrue(q1.contains(Instant.parse("2026-03-31T23:59:59Z")));
        assertFalse(q1.contains(T1), "to endpoint is excluded");
        assertFalse(q1.contains(Instant.parse("2025-12-31T23:59:59Z")));
    }

    @Test
    void openEndedIntervalContainsEveryLaterInstant() {
        Interval open = Interval.startingAt(T0);

        assertNull(open.to());
        assertTrue(open.contains(Instant.parse("2099-12-31T00:00:00Z")));
        assertFalse(open.contains(Instant.parse("2025-12-31T23:59:59Z")));
        assertEquals(Instant.MAX, open.endOrMax());
    }

    @Test
    void adjacentHalfOpenIntervalsDoNotOverlap() {
        Interval q1 = Interval.of(T0, T1);
        Interval q2 = Interval.of(T1, T2);

        assertFalse(q1.overlaps(q2), "[a,b) and [b,c) share only the excluded b");
        assertTrue(q1.overlaps(Interval.of(Instant.parse("2026-03-31T23:59:59Z"), T2)));
        assertTrue(q1.overlaps(Interval.startingAt(T1.minusSeconds(1))),
                "one second of real intersection overlaps");
    }

    @Test
    void rejectsNullFromAndNonPositiveLength() {
        assertThrows(IllegalArgumentException.class, () -> Interval.of(null, T1));
        assertThrows(IllegalArgumentException.class, () -> Interval.of(T1, T1),
                "zero-length interval is not valid");
        assertThrows(IllegalArgumentException.class, () -> Interval.of(T2, T1),
                "inverted interval is not valid");
    }
}
