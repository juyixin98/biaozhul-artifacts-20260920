package com.example.wm.model;

import java.util.List;

/**
 * 协调器整体快照，用于查询/序列化输出。
 *
 * @param processingTimeMs 当前处理时间（注入时钟读数）
 * @param globalWatermark  全局水位线（无活跃分区时为 null）
 * @param activeCount      活跃分区数
 * @param idleCount        空闲分区数
 * @param pausedCount      显式暂停分区数
 * @param partitions       各分区状态视图（按分区名排序）
 * @param lateEventCount   累计进入迟到通道的事件数
 */
public record CoordinationSnapshot(
        long processingTimeMs,
        Long globalWatermark,
        int activeCount,
        int idleCount,
        int pausedCount,
        List<PartitionStateView> partitions,
        long lateEventCount
) {
}
