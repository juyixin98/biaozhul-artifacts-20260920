package com.example.monotime.domain;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 墙钟域规则解析：绝对瞬间、相对时长、每日本地时刻（含夏令时切换日）。
 */
class DeadlineCalculatorTest {

    private final DeadlineCalculator calculator = new DeadlineCalculator();

    @Test
    void absoluteInstantRuleReturnsItsInstant() {
        // Arrange
        Instant fixed = Instant.parse("2026-11-01T17:30:00Z");
        TimeRule rule = new TimeRule.AbsoluteInstant("a", "1", fixed, "");

        // Act + Assert：与安排时刻无关
        assertEquals(fixed, calculator.deadline(rule, Instant.parse("2026-01-01T00:00:00Z")));
    }

    @Test
    void relativeDurationAddsToScheduleInstant() {
        // Arrange
        Instant now = Instant.parse("2026-09-25T09:00:00Z");
        TimeRule rule = new TimeRule.RelativeDuration("r", "1", java.time.Duration.parse("PT2M"));

        // Act + Assert
        assertEquals(Instant.parse("2026-09-25T09:02:00Z"), calculator.deadline(rule, now));
    }

    @Test
    void dailyCutoffTodayWhenNotYetReached() {
        // Arrange：上海 09:00（UTC 01:00），截止本地 18:00 → 当天 10:00Z
        Instant now = Instant.parse("2026-09-25T01:00:00Z");
        TimeRule rule = new TimeRule.DailyLocalCutoff("d", "1", LocalTime.of(18, 0), ZoneId.of("Asia/Shanghai"));

        // Act + Assert
        assertEquals(Instant.parse("2026-09-25T10:00:00Z"), calculator.deadline(rule, now));
    }

    @Test
    void dailyCutoffRollsToNextDayWhenAlreadyPassed() {
        // Arrange：上海本地 19:00（UTC 11:00），今日 18:00 已过 → 明天
        Instant now = Instant.parse("2026-09-25T11:00:00Z");
        TimeRule rule = new TimeRule.DailyLocalCutoff("d", "1", LocalTime.of(18, 0), ZoneId.of("Asia/Shanghai"));

        // Act + Assert
        assertEquals(Instant.parse("2026-09-26T10:00:00Z"), calculator.deadline(rule, now));
    }

    @Test
    void dailyCutoffRespectsDstFallBackInNewYork() {
        // Arrange：2026-11-01 是美东夏令时结束日（02:00 本地回拨一小时，UTC-4 → UTC-5）。
        // 当天 UTC 20:00（本地 16:00，冬令时 UTC-5）安排，截止本地 17:30 → 22:30Z。
        Instant now = Instant.parse("2026-11-01T20:00:00Z");
        TimeRule rule = new TimeRule.DailyLocalCutoff("d", "1", LocalTime.of(17, 30), ZoneId.of("America/New_York"));

        // Act
        Instant deadline = calculator.deadline(rule, now);

        // Assert：冬令时生效，17:30 本地 = 22:30Z（若是夏令时会是 21:30Z）
        assertEquals(Instant.parse("2026-11-01T22:30:00Z"), deadline);
    }

    @Test
    void dailyCutoffBeforeFallBackUsesDaylightOffset() {
        // Arrange：同一天 UTC 05:00（本地 01:00，仍为夏令时 UTC-4），截止 01:30 本地 → 05:30Z
        Instant now = Instant.parse("2026-11-01T05:00:00Z");
        TimeRule rule = new TimeRule.DailyLocalCutoff("d", "1", LocalTime.of(1, 30), ZoneId.of("America/New_York"));

        // Act + Assert
        Instant deadline = calculator.deadline(rule, now);
        assertEquals(Instant.parse("2026-11-01T05:30:00Z"), deadline);
        assertTrue(deadline.isAfter(now));
    }

    @Test
    void exactCutoffInstantRollsToNextDay() {
        // Arrange：恰好等于本地截止时刻（含等号 → 已过，顺延）
        Instant now = Instant.parse("2026-09-25T10:00:00Z");
        TimeRule rule = new TimeRule.DailyLocalCutoff("d", "1", LocalTime.of(18, 0), ZoneId.of("Asia/Shanghai"));

        // Act + Assert
        assertEquals(Instant.parse("2026-09-26T10:00:00Z"), calculator.deadline(rule, now));
    }
}
