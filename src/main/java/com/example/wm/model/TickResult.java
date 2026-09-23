package com.example.wm.model;

import java.util.List;

/**
 * 一次时钟推进（tick）后的协调结果。
 *
 * @param processingTimeMs        tick 使用的处理时间
 * @param timedOutPartitions      本次因空闲超时新转为 IDLE 的分区
 * @param previousGlobalWatermark tick 前的全局水位线
 * @param globalWatermark         tick 后的全局水位线
 */
public record TickResult(
        long processingTimeMs,
        List<String> timedOutPartitions,
        Long previousGlobalWatermark,
        Long globalWatermark
) {
}
