package com.example.monotime.domain;

import com.example.monotime.SimulatedClock;
import com.example.monotime.tzdb.TzdbInfo;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 核心验收：墙钟向前/向后校时不改变已安排超时；只有单调流逝才改变剩余时间。
 */
class MonotonicConverterTest {

    private static final Instant START_WALL = Instant.parse("2026-09-25T09:00:00Z");
    private static final long START_TICK = 1_000_000_000L;

    private final SimulatedClock clock = new SimulatedClock(START_WALL, START_TICK);
    private final MonotonicConverter converter =
            new MonotonicConverter(clock, new DeadlineCalculator(), TzdbInfo.detect().tzdbVersion());

    @Test
    void convertsWallDeadlineToMonotonicTickOnceAtSchedule() {
        // Arrange
        TimeRule rule = new TimeRule.RelativeDuration("grace-two-minutes", "1", Duration.ofMinutes(10));

        // Act
        ScheduledTimeout timeout = converter.schedule("t1", rule);
        TimeoutStatus status = converter.statusOf(timeout);

        // Assert
        assertEquals(START_WALL.plus(Duration.ofMinutes(10)), timeout.deadlineInstant());
        assertEquals(Duration.ofMinutes(10).toNanos(), timeout.durationAtScheduleNanos());
        assertEquals(START_TICK + Duration.ofMinutes(10).toNanos(), timeout.monotonicDeadlineTickNanos());
        assertEquals(Duration.ofMinutes(10).toNanos(), status.remainingNanos());
        assertFalse(status.expired());
        assertFalse(timeout.alreadyExpiredAtSchedule());
    }

    @Test
    void backwardWallClockAdjustmentDoesNotChangeScheduledTimeout() {
        // Arrange：安排一个 10 分钟超时
        ScheduledTimeout timeout = converter.schedule("t1",
                new TimeRule.RelativeDuration("g", "1", Duration.ofMinutes(10)));

        // Act：管理员把墙钟向后拨 2 小时（单调刻度不动）
        clock.jumpWallClockBackward(Duration.ofHours(2));
        TimeoutStatus status = converter.statusOf(timeout);

        // Assert：剩余时间仍是 10 分钟，超时未被推迟
        assertEquals(Duration.ofMinutes(10).toNanos(), status.remainingNanos());
        assertFalse(status.expired());
    }

    @Test
    void forwardWallClockAdjustmentDoesNotExpireScheduledTimeout() {
        // Arrange
        ScheduledTimeout timeout = converter.schedule("t1",
                new TimeRule.RelativeDuration("g", "1", Duration.ofMinutes(10)));

        // Act：NTP 把墙钟向前跳 1 天（单调刻度不动）
        clock.jumpWallClockForward(Duration.ofDays(1));
        TimeoutStatus status = converter.statusOf(timeout);

        // Assert：墙钟显示已过一天，但单调计时器说还有 10 分钟——不能误判过期
        assertEquals(Duration.ofMinutes(10).toNanos(), status.remainingNanos());
        assertFalse(status.expired());
    }

    @Test
    void monotonicElapseChangesRemainingAndWallAdjustmentAfterwardsDoesNot() {
        // Arrange
        ScheduledTimeout timeout = converter.schedule("t1",
                new TimeRule.RelativeDuration("g", "1", Duration.ofMinutes(10)));

        // Act + Assert：真实流逝 4 分钟 → 剩余 6 分钟
        clock.advanceMonotonic(Duration.ofMinutes(4));
        assertEquals(Duration.ofMinutes(6).toNanos(), converter.statusOf(timeout).remainingNanos());

        // Act + Assert：再把墙钟向前/向后任意调整 → 剩余仍是 6 分钟
        clock.jumpWallClockForward(Duration.ofHours(8));
        assertEquals(Duration.ofMinutes(6).toNanos(), converter.statusOf(timeout).remainingNanos());
        clock.jumpWallClockBackward(Duration.ofHours(8));
        assertEquals(Duration.ofMinutes(6).toNanos(), converter.statusOf(timeout).remainingNanos());

        // Act + Assert：单调流逝累计达到 10 分钟 → 过期
        clock.advanceMonotonic(Duration.ofMinutes(6));
        TimeoutStatus expired = converter.statusOf(timeout);
        assertTrue(expired.expired());
        assertTrue(expired.remainingNanos() <= 0);
    }

    @Test
    void marksAlreadyExpiredDeadlineAtScheduleTime() {
        // Arrange：绝对截止时间在安排时刻之前 30 秒
        TimeRule rule = new TimeRule.AbsoluteInstant(
                "past", "1", START_WALL.minusSeconds(30), "已过期截止");

        // Act
        ScheduledTimeout timeout = converter.schedule("late", rule);
        TimeoutStatus status = converter.statusOf(timeout);

        // Assert
        assertTrue(timeout.alreadyExpiredAtSchedule());
        assertEquals(-Duration.ofSeconds(30).toNanos(), timeout.durationAtScheduleNanos());
        assertTrue(status.expired());
    }

    @Test
    void marksDeadlineExactlyAtScheduleTimeAsExpired() {
        // Arrange
        TimeRule rule = new TimeRule.AbsoluteInstant("edge", "1", START_WALL, "恰好等于安排时刻");

        // Act
        ScheduledTimeout timeout = converter.schedule("edge", rule);

        // Assert：时长为 0 即视为已到期（边界含等号）
        assertEquals(0L, timeout.durationAtScheduleNanos());
        assertTrue(timeout.alreadyExpiredAtSchedule());
        assertTrue(converter.statusOf(timeout).expired());
    }

    @Test
    void recoverAfterRestartReanchorsToNewMonotonicEpochUsingWallDeadline() {
        // Arrange：安排 10 分钟绝对超时，随后 JVM 崩溃
        TimeRule rule = new TimeRule.AbsoluteInstant(
                "abs", "1", START_WALL.plus(Duration.ofMinutes(10)), "");
        ScheduledTimeout beforeCrash = converter.schedule("t1", rule);

        // Act：重启。nanoTime 纪元重置为任意新读数 999_000_000_000；
        // 重启时墙钟已自然走过 3 分钟（剩余 7 分钟）
        long newEpochTick = 999_000_000_000L;
        Instant wallAtRecovery = START_WALL.plus(Duration.ofMinutes(3));
        ScheduledTimeout recovered = converter.recoverAfterRestart(beforeCrash, newEpochTick, wallAtRecovery);
        TimeoutStatus status = converter.statusAt(recovered, newEpochTick);

        // Assert：墙钟截止瞬间不变，单调死线按新纪元重算，剩余 7 分钟
        assertEquals(beforeCrash.deadlineInstant(), recovered.deadlineInstant());
        assertTrue(recovered.recoveredAfterRestart());
        assertEquals(newEpochTick + Duration.ofMinutes(7).toNanos(),
                recovered.monotonicDeadlineTickNanos());
        assertEquals(Duration.ofMinutes(7).toNanos(), status.remainingNanos());
        assertFalse(status.expired());
    }

    @Test
    void recoveredTimeoutStillExpiresByMonotonicTimeOnly() {
        // Arrange：重启恢复时剩余 7 分钟
        ScheduledTimeout beforeCrash = converter.schedule("t1", new TimeRule.AbsoluteInstant(
                "abs", "1", START_WALL.plus(Duration.ofMinutes(10)), ""));
        long newEpochTick = 5_000_000_000L;
        ScheduledTimeout recovered = converter.recoverAfterRestart(
                beforeCrash, newEpochTick, START_WALL.plus(Duration.ofMinutes(3)));

        // Act：流逝 7 分钟，期间墙钟被向后拨——过期判断只看单调刻度
        clock.setMonotonicNanos(newEpochTick);
        clock.advanceMonotonic(Duration.ofMinutes(7));
        clock.jumpWallClockBackward(Duration.ofDays(30));

        // Assert
        assertTrue(converter.statusOf(recovered).expired());
    }

    @Test
    void recordsTzdbVersionOnEveryScheduledTimeout() {
        // Arrange
        String expected = TzdbInfo.detect().tzdbVersion();

        // Act
        ScheduledTimeout timeout = converter.schedule("t1",
                new TimeRule.RelativeDuration("g", "1", Duration.ofMinutes(1)));

        // Assert
        assertEquals(expected, timeout.tzdbVersion());
        assertTrue(timeout.tzdbVersion().matches("\\d{4}[a-z]?"));
    }
}
