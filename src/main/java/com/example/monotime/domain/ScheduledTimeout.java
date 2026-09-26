package com.example.monotime.domain;

import java.time.Duration;
import java.time.Instant;

/**
 * 一次已安排超时的运行时状态。同时持有墙钟域与单调域数据，但两域各管一段：
 *
 * <ul>
 *   <li>{@code deadlineInstant} / {@code scheduledAtInstant}：墙钟域，仅用于展示、持久化、
 *       以及重启恢复时在转换边界上重新锚定；</li>
 *   <li>{@code monotonicDeadlineTickNanos} / {@code scheduledAtTickNanos}：单调域，
 *       运行期间所有到期判断只用它们。</li>
 * </ul>
 *
 * @param durationAtScheduleNanos 安排瞬间按两墙钟瞬间之差算出的时长（纳秒）
 * @param alreadyExpiredAtSchedule 安排时截止时间已过（时长 ≤ 0），立即到期
 * @param recoveredAfterRestart 是否由持久化记录在重启后重新锚定生成
 */
public record ScheduledTimeout(
        String timeoutId,
        String ruleId,
        String ruleVersion,
        Instant scheduledAtInstant,
        Instant deadlineInstant,
        long durationAtScheduleNanos,
        long scheduledAtTickNanos,
        long monotonicDeadlineTickNanos,
        boolean alreadyExpiredAtSchedule,
        boolean recoveredAfterRestart,
        String tzdbVersion) {

    public Duration durationAtSchedule() {
        return Duration.ofNanos(durationAtScheduleNanos);
    }
}
