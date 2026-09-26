package com.example.timeout.api;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;

/**
 * 超时条目的 JSON 视图。
 *
 * <p>墙钟字段与单调字段同时返回，但通过命名明确区分：
 * {@code *Wall} 是日历时间（可被校时改变），{@code *MonoNanos} 是进程内单调读数（重启作废）。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record TimeoutJson(
        String id,
        String label,
        String status,
        Instant deadlineWall,
        Instant convertedAtWall,
        Long convertedAtMonoNanos,
        Long remainingAtConversionNanos,
        Long fireMonoNanos,
        Long remainingMonotonicNanos,
        Instant firedAtWall,
        Long firedAtMonoNanos,
        String ruleSource
) {
}
