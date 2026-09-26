package com.example.monotime.domain;

import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;

/**
 * 把规则解析为安排时刻之后的下一个墙钟截止瞬间（UTC 时间线上的绝对点）。
 *
 * <p>这是“转换边界”上的墙钟域运算：输入输出全是 {@link Instant}，
 * 绝不与单调刻度混算。夏令时按 JDK 内置 TZDB 规则解析。</p>
 */
public final class DeadlineCalculator {

    /**
     * 每日本地截止：若今天的时刻已过（含恰好相等），则顺延到下一次出现（明天）。
     *
     * <p>夏令时歧义策略（由 {@code LocalDateTime.atZone} 固定）：春令时“跳空”的不存在
     * 本地时刻向后顺延到缺口之后；秋令时“重叠”的重复本地时刻取较早偏移（夏令时那一次）。
     * 固定目录中的 17:30/18:00 不会落入缺口/重叠区间。</p>
     */
    public Instant deadline(TimeRule rule, Instant scheduledAt) {
        return switch (rule) {
            case TimeRule.AbsoluteInstant a -> a.instant();
            case TimeRule.RelativeDuration r -> scheduledAt.plus(r.duration());
            case TimeRule.DailyLocalCutoff d -> nextDailyOccurrence(d, scheduledAt);
        };
    }

    private Instant nextDailyOccurrence(TimeRule.DailyLocalCutoff daily, Instant scheduledAt) {
        ZoneId zone = daily.zoneId();
        ZonedDateTime nowInZone = scheduledAt.atZone(zone);
        ZonedDateTime today = nowInZone.toLocalDate().atTime(daily.localTime()).atZone(zone);
        ZonedDateTime occurrence = today.isAfter(nowInZone) ? today : today.plusDays(1);
        return occurrence.toInstant();
    }
}
