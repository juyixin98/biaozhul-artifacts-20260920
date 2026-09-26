package com.example.timeout.rule;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;

/**
 * 截止时间规则：描述“墙钟截止时间怎么定”。规则只产出墙钟瞬时，
 * 之后由 {@code TimeoutRegistry} 在当前时刻统一完成到单调计时器的转换。
 *
 * <ul>
 *   <li>{@link Absolute}：直接给定 UTC 瞬时；</li>
 *   <li>{@link DurationRule}：从当前墙钟起经过固定时长；</li>
 *   <li>{@link DailyLocal}：某时区下每天固定本地时刻（正确处理 DST gap/overlap）；</li>
 *   <li>{@link VersionTtl}：版本规则——截止时间 = 版本发布时间戳 + TTL，
 *       与“何时安排”无关，因此同一版本重放结果确定，安排时可能已过期。</li>
 * </ul>
 */
public sealed interface DeadlineRule
        permits DeadlineRule.Absolute, DeadlineRule.DurationRule,
                DeadlineRule.DailyLocal, DeadlineRule.VersionTtl {

    record Absolute(Instant instant) implements DeadlineRule {
        public Absolute {
            Rules.require(instant, "instant");
        }
    }

    record DurationRule(Duration duration) implements DeadlineRule {
        public DurationRule {
            Rules.require(duration, "duration");
            Rules.requirePositive(duration);
        }
    }

    record DailyLocal(LocalTime localTime, ZoneId zone) implements DeadlineRule {
        public DailyLocal {
            Rules.require(localTime, "localTime");
            Rules.require(zone, "zone");
        }
    }

    /**
     * 版本规则：{@code issuedAt} 是版本（数据版本/配置版本）产生的墙钟时间戳，
     * {@code ttl} 是该版本的有效期。版本一产生，截止时刻就已客观确定。
     */
    record VersionTtl(Instant issuedAt, Duration ttl) implements DeadlineRule {
        public VersionTtl {
            Rules.require(issuedAt, "issuedAt");
            Rules.require(ttl, "ttl");
            Rules.requirePositive(ttl);
        }
    }

    static Absolute absolute(Instant instant) {
        return new Absolute(instant);
    }

    static DurationRule duration(Duration duration) {
        return new DurationRule(duration);
    }

    static DailyLocal dailyLocal(LocalTime localTime, ZoneId zone) {
        return new DailyLocal(localTime, zone);
    }

    static VersionTtl versionTtl(Instant issuedAt, Duration ttl) {
        return new VersionTtl(issuedAt, ttl);
    }

    /** 规则入参统一在边界处校验，错误一律以 IllegalArgumentException 暴露。 */
    final class Rules {
        private Rules() {
        }

        static void require(Object value, String name) {
            if (value == null) {
                throw new IllegalArgumentException(name + " must not be null");
            }
        }

        static void requirePositive(Duration d) {
            if (d.isNegative() || d.isZero()) {
                throw new IllegalArgumentException("duration must be positive: " + d);
            }
        }
    }
}
