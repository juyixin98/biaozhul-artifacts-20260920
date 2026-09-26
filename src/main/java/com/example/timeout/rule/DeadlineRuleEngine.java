package com.example.timeout.rule;

import com.example.timeout.clock.TimeoutClock;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZonedDateTime;

/**
 * 把 {@link DeadlineRule} 解析为具体墙钟截止瞬时。
 *
 * <p>这里读取 {@link TimeoutClock#wall()} 只为了回答“下一次是几点”，
 * 不做任何时长计时；时长计时是 {@code TimeoutRegistry} 的单调钟职责。
 */
public final class DeadlineRuleEngine {

    public Instant resolve(DeadlineRule rule, TimeoutClock clock) {
        var now = clock.wall();
        return switch (rule) {
            case DeadlineRule.Absolute a -> a.instant();
            case DeadlineRule.DurationRule d -> now.plus(d.duration());
            case DeadlineRule.VersionTtl v -> v.issuedAt().plus(v.ttl());
            case DeadlineRule.DailyLocal daily -> nextDailyOccurrence(daily, now);
        };
    }

    /**
     * 求时区 {@code zone} 中今天/明天的 {@code localTime}：
     * 今天该时刻仍晚于 {@code now} 就取今天，否则取明天。
     * DST gap 由 {@link ZonedDateTime} 按该时区规则自动顺延；
     * overlap（秋季回拨）取较早一次（withEarlierOffsetAtOverlap），语义确定。
     */
    private Instant nextDailyOccurrence(DeadlineRule.DailyLocal daily, Instant now) {
        ZonedDateTime nowZoned = now.atZone(daily.zone());
        LocalDate today = nowZoned.toLocalDate();
        ZonedDateTime candidate = daily.localTime()
                .atDate(today)
                .atZone(daily.zone())
                .withEarlierOffsetAtOverlap();
        if (!candidate.toInstant().isAfter(now)) {
            candidate = daily.localTime()
                    .atDate(today.plusDays(1))
                    .atZone(daily.zone())
                    .withEarlierOffsetAtOverlap();
        }
        return candidate.toInstant();
    }
}
