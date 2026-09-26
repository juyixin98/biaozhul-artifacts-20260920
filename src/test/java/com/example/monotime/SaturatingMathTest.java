package com.example.monotime;

import org.junit.jupiter.api.Test;

import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SaturatingMathTest {

    @Test
    void addClampsAtLongBounds() {
        assertEquals(Long.MAX_VALUE, SaturatingMath.addClamped(Long.MAX_VALUE, 1));
        assertEquals(Long.MIN_VALUE, SaturatingMath.addClamped(Long.MIN_VALUE, -1));
        assertEquals(3L, SaturatingMath.addClamped(1L, 2L));
    }

    @Test
    void subtractClampsAtLongBounds() {
        assertEquals(Long.MIN_VALUE, SaturatingMath.subtractClamped(Long.MIN_VALUE, 1));
        assertEquals(Long.MAX_VALUE, SaturatingMath.subtractClamped(Long.MAX_VALUE, -1));
        assertEquals(1L, SaturatingMath.subtractClamped(3L, 2L));
    }

    @Test
    void durationToNanosSaturatesInsteadOfOverflowing() {
        assertEquals(Long.MAX_VALUE, SaturatingMath.toNanosSaturated(Duration.ofDays(365 * 1000L)));
        assertEquals(Long.MIN_VALUE, SaturatingMath.toNanosSaturated(Duration.ofDays(-365 * 1000L)));
        assertEquals(1_000_000_000L, SaturatingMath.toNanosSaturated(Duration.ofSeconds(1)));
    }
}
