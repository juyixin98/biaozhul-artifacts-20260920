package com.example.monotime.domain;

/**
 * 某一时刻对超时的查询结果。完全基于单调刻度计算，
 * 因此查询瞬间的墙钟值即便被向前/向后调整，剩余时间也不受影响。
 *
 * @param remainingNanos 剩余纳秒（饱和钳制）；过期后为 {@code Long.MIN_VALUE}
 */
public record TimeoutStatus(
        String timeoutId,
        long nowTickNanos,
        long monotonicDeadlineTickNanos,
        long remainingNanos,
        boolean expired) {
}
