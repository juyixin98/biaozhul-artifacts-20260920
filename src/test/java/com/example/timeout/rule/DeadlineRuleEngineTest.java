package com.example.timeout.rule;

import com.example.timeout.clock.VirtualClock;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DeadlineRuleEngineTest {

    private static final Instant T0 = Instant.parse("2026-03-28T10:00:00Z");

    private final DeadlineRuleEngine engine = new DeadlineRuleEngine();

    @Test
    void absoluteRuleReturnsGivenInstant() {
        var rule = DeadlineRule.absolute(Instant.parse("2026-04-01T00:00:00Z"));
        assertEquals(Instant.parse("2026-04-01T00:00:00Z"),
                engine.resolve(rule, new VirtualClock(T0)));
    }

    @Test
    void durationRuleAddsToCurrentWallClock() {
        var rule = DeadlineRule.duration(Duration.ofMinutes(90));
        assertEquals(Instant.parse("2026-03-28T11:30:00Z"),
                engine.resolve(rule, new VirtualClock(T0)));
    }

    @Test
    void dailyLocalRuleResolvesNextOccurrenceInZone() {
        // 10:00Z = 18:00 上海；今天 03:30 已过 -> 次日 03:30 上海 = 前一天 19:30Z
        var rule = DeadlineRule.dailyLocal(LocalTime.of(3, 30), ZoneId.of("Asia/Shanghai"));
        Instant d = engine.resolve(rule, new VirtualClock(T0));
        assertEquals(Instant.parse("2026-03-28T19:30:00Z"), d);

        // 上午（UTC 18:00 = 上海次日 02:00）：今天 03:30 还没到
        Instant night = Instant.parse("2026-03-28T18:00:00Z");
        Instant d2 = engine.resolve(rule, new VirtualClock(night));
        assertEquals(Instant.parse("2026-03-28T19:30:00Z"), d2);
    }

    @Test
    void dailyLocalRuleSurvivesSpringForwardDstGap() {
        // America/New_York 2026-03-08 02:00 不存在（春令时跳变）。
        // 02:30 落在 gap 中，java.time 按 gap 规则顺延到 03:30-04:00（EDT, UTC-4）。
        var rule = DeadlineRule.dailyLocal(LocalTime.of(2, 30), ZoneId.of("America/New_York"));
        Instant justBefore = Instant.parse("2026-03-08T04:00:00Z"); // 当地 23:00 (前一天)
        Instant d = engine.resolve(rule, new VirtualClock(justBefore));
        // 下一次 02:30 本地 = gap，被调整为 03:30 EDT = 07:30Z
        assertEquals(Instant.parse("2026-03-08T07:30:00Z"), d);
    }

    @Test
    void versionTtlRuleExpiresTtlAfterTheVersionTimestamp() {
        // 版本时间戳决定截止时刻，而不是“安排时的现在”：
        // 重放同一版本必须得到同一截止时间（确定性）。
        Instant v1Issued = Instant.parse("2026-03-28T09:00:00Z");
        var rule = DeadlineRule.versionTtl(v1Issued, Duration.ofMinutes(15));

        VirtualClock clock = new VirtualClock(T0); // 10:00Z 才安排
        Instant d = engine.resolve(rule, clock);
        assertEquals(Instant.parse("2026-03-28T09:15:00Z"), d);
        // 该版本在安排时已经过期：截止时间落在过去
        assertTrue(d.isBefore(clock.wall()));
    }

    @Test
    void rejectsNullRuleInputs() {
        assertThrows(IllegalArgumentException.class, () -> DeadlineRule.duration(null));
        assertThrows(IllegalArgumentException.class,
                () -> DeadlineRule.versionTtl(null, Duration.ofSeconds(1)));
        assertThrows(IllegalArgumentException.class,
                () -> DeadlineRule.versionTtl(T0, null));
        assertThrows(IllegalArgumentException.class,
                () -> DeadlineRule.dailyLocal(null, ZoneId.of("UTC")));
    }
}
