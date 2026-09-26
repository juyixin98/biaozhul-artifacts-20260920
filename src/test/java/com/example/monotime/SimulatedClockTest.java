package com.example.monotime;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;

/** 虚拟时钟：校时与流逝是两个正交操作——这是全部验收场景的可控基础。 */
class SimulatedClockTest {

    @Test
    void wallAdjustmentLeavesMonotonicTickUntouched() {
        // Arrange
        SimulatedClock clock = new SimulatedClock(Instant.parse("2026-09-25T09:00:00Z"), 42L);

        // Act
        clock.jumpWallClockForward(Duration.ofHours(5));

        // Assert
        assertEquals(42L, clock.monotonicNanos());
        assertEquals(Instant.parse("2026-09-25T14:00:00Z"), clock.wallClockInstant());
    }

    @Test
    void backwardWallAdjustmentLeavesMonotonicTickUntouched() {
        // Arrange
        SimulatedClock clock = new SimulatedClock(Instant.parse("2026-09-25T09:00:00Z"), 42L);

        // Act
        clock.jumpWallClockBackward(Duration.ofMinutes(90));

        // Assert
        assertEquals(42L, clock.monotonicNanos());
        assertEquals(Instant.parse("2026-09-25T07:30:00Z"), clock.wallClockInstant());
    }

    @Test
    void monotonicAdvanceLeavesWallClockUntouched() {
        // Arrange
        SimulatedClock clock = new SimulatedClock(Instant.parse("2026-09-25T09:00:00Z"), 0L);

        // Act
        clock.advanceMonotonic(Duration.ofMinutes(3));

        // Assert
        assertEquals(Instant.parse("2026-09-25T09:00:00Z"), clock.wallClockInstant());
        assertEquals(Duration.ofMinutes(3).toNanos(), clock.monotonicNanos());
    }

    @Test
    void epochCanBeResetIndependentlyOnRestart() {
        // Arrange
        SimulatedClock clock = new SimulatedClock(Instant.parse("2026-09-25T09:00:00Z"), 1_000L);

        // Act：重启后 nanoTime 从完全不同的读数重新起算
        clock.setMonotonicNanos(7L);

        // Assert
        assertEquals(7L, clock.monotonicNanos());
        assertNotEquals(1_000L, clock.monotonicNanos());
    }
}
