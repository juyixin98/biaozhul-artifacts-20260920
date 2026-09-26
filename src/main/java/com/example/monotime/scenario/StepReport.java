package com.example.monotime.scenario;

import java.time.Instant;

/**
 * 单步执行结果。
 *
 * @param index 步骤序号（从 0 起）
 * @param op 操作名
 * @param note 步骤说明
 * @param wallClockAfter 操作后墙钟
 * @param monotonicNanosAfter 操作后单调刻度
 * @param detail 操作细节/返回值
 * @param error 失败信息（成功为 null）
 */
public record StepReport(
        int index,
        String op,
        String note,
        Instant wallClockBefore,
        Instant wallClockAfter,
        long monotonicNanosBefore,
        long monotonicNanosAfter,
        Object detail,
        String error) {
}
