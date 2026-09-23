package com.example.wm.model;

/**
 * 单个分区的运行时状态快照。
 *
 * @param partition            分区标识
 * @param status               当前状态
 * @param lastEventTimeMs      最后一次事件的处理时间（从未收到事件为 null）
 * @param maxEventTimeMs       已观察到的最大事件时间（从未收到事件为 null）
 * @param localWatermarkMs     本分区自己的水位线（maxEventTime − B，从未收到事件为 null）
 * @param effectiveWatermarkMs 参与全局聚合的有效水位线（见引擎说明；从未收到事件为 null）
 * @param seenEvents           已收事件数
 * @param lateEvents           被判定为迟到的事件数
 */
public record PartitionStateView(
        String partition,
        PartitionStatus status,
        Long lastEventTimeMs,
        Long maxEventTimeMs,
        Long localWatermarkMs,
        Long effectiveWatermarkMs,
        long seenEvents,
        long lateEvents
) {
}
