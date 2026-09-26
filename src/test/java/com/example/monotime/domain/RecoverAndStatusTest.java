package com.example.monotime.domain;

import com.example.monotime.SimulatedClock;
import com.example.monotime.tzdb.TzdbInfo;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** recoverAndStatus：在同一时钟快照上恢复锚定并立即查询。 */
class RecoverAndStatusTest {

    @Test
    void recoversAndReportsStatusFromOneClockSnapshot() {
        // Arrange：截止 = 起始墙钟 + 10 分钟
        Instant startWall = Instant.parse("2026-09-25T09:00:00Z");
        SimulatedClock clock = new SimulatedClock(startWall.plus(Duration.ofMinutes(4)), 7_000_000_000L);
        MonotonicConverter converter =
                new MonotonicConverter(clock, new DeadlineCalculator(), TzdbInfo.detect().tzdbVersion());
        ScheduledTimeout persistedWallFields = new ScheduledTimeout(
                "t1", "rule", "1",
                startWall, startWall.plus(Duration.ofMinutes(10)),
                Long.MAX_VALUE, Long.MAX_VALUE, Long.MAX_VALUE, true, false, "2026b");

        // Act：单调字段（MAX_VALUE 等）必须被忽略重算
        TimeoutStatus status = converter.recoverAndStatus(persistedWallFields);

        // Assert：恢复时已过 4 分钟 → 剩余 6 分钟；nowTick 取同一快照 7_000_000_000
        assertEquals(Duration.ofMinutes(6).toNanos(), status.remainingNanos());
        assertEquals(7_000_000_000L, status.nowTickNanos());
        assertFalse(status.expired());
    }

    @Test
    void recoverAndStatusDetectsAlreadyPassedDeadline() {
        // Arrange
        Instant startWall = Instant.parse("2026-09-25T09:00:00Z");
        SimulatedClock clock = new SimulatedClock(startWall.plus(Duration.ofMinutes(11)), 0L);
        MonotonicConverter converter =
                new MonotonicConverter(clock, new DeadlineCalculator(), TzdbInfo.detect().tzdbVersion());
        ScheduledTimeout persistedWallFields = new ScheduledTimeout(
                "t1", "rule", "1", startWall, startWall.plus(Duration.ofMinutes(10)),
                0L, 0L, 0L, false, false, TzdbInfo.detect().tzdbVersion());

        // Act
        TimeoutStatus status = converter.recoverAndStatus(persistedWallFields);

        // Assert
        assertTrue(status.expired());
        assertTrue(status.remainingNanos() < 0);
    }
}
