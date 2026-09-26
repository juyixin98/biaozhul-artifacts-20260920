package com.example.monotime.persistence;

import java.time.Instant;

/**
 * 持久化记录：刻意只含墙钟域与审计字段，不含任何单调刻度。
 *
 * @param scheduledTzdbVersion 安排时使用的 TZDB 版本（审计用）
 */
public record PersistedTimeout(
        String timeoutId,
        String ruleId,
        String ruleVersion,
        Instant scheduledAtInstant,
        Instant deadlineInstant,
        String scheduledTzdbVersion) {
}
