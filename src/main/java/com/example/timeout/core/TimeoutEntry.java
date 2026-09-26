package com.example.timeout.core;

import java.time.Instant;

/**
 * 一个超时条目的不可变快照。
 *
 * <p>并存两类字段，但语义完全不同，任何时候都不得互换使用：
 * <ul>
 *   <li>{@code deadlineWall}：墙钟截止时间——来自业务输入，是持久化与重启重算的唯一依据；</li>
 *   <li>{@code fireMonoNanos}：单调触发读数——只在本进程内有效，进程重启即作废，必须重算。</li>
 * </ul>
 */
public record TimeoutEntry(
        String id,
        String label,
        Instant deadlineWall,
        Instant convertedAtWall,
        long convertedAtMonoNanos,
        long remainingAtConversionNanos,
        long fireMonoNanos,
        TimeoutStatus status,
        Instant firedAtWall,
        Long firedAtMonoNanos,
        String ruleSource
) {

    public boolean isExpired() {
        return status == TimeoutStatus.EXPIRED;
    }

    /** 到期后生成过期副本，记录触发瞬间两个时钟各自的读数。 */
    public TimeoutEntry toExpired(Instant firedWall, long firedMono) {
        return new TimeoutEntry(id, label, deadlineWall, convertedAtWall, convertedAtMonoNanos,
                remainingAtConversionNanos, fireMonoNanos, TimeoutStatus.EXPIRED,
                firedWall, firedMono, ruleSource);
    }
}
