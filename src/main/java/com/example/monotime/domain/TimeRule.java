package com.example.monotime.domain;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;

/**
 * 时间规则（按 ruleId + version 从固定目录中选取）。三种形态：
 *
 * <ul>
 *   <li>{@link AbsoluteInstant}：固定的墙钟截止瞬间（含跨夏令时切换的绝对时间点）。</li>
 *   <li>{@link RelativeDuration}：自安排时刻起经过固定时长（单调时长语义）。</li>
 *   <li>{@link DailyLocalCutoff}：某时区每天的本地时刻截止（用 TZDB 解析当天偏移，含夏令时）。</li>
 * </ul>
 */
public sealed interface TimeRule
        permits TimeRule.AbsoluteInstant, TimeRule.RelativeDuration, TimeRule.DailyLocalCutoff {

    String ruleId();

    String version();

    /** 固定 UTC 截止瞬间。 */
    record AbsoluteInstant(String ruleId, String version, Instant instant, String note) implements TimeRule {
    }

    /** 相对安排时刻的时长。 */
    record RelativeDuration(String ruleId, String version, Duration duration) implements TimeRule {
    }

    /** 某时区每天的本地截止时刻。 */
    record DailyLocalCutoff(String ruleId, String version, LocalTime localTime, ZoneId zoneId) implements TimeRule {
    }
}
