package com.example.wm.model;

/**
 * 一次事件注入的结果。
 *
 * @param accepted                 是否按准时接受；false 表示进入迟到通道
 * @param classification           ON_TIME / LATE
 * @param reason                   迟到原因（准时为 null）
 * @param resumedFromIdle          该分区此前是否处于 IDLE/PAUSED，本次注入将其重新激活
 * @param previousGlobalWatermark  注入前的全局水位线（无则为 null）
 * @param globalWatermark          注入后的全局水位线（无活跃分区时为 null）
 * @param timedOutPartitions       本次注入触发空闲超时而转为 IDLE 的分区
 */
public record IngestionResult(
        boolean accepted,
        Classification classification,
        LateReason reason,
        boolean resumedFromIdle,
        Long previousGlobalWatermark,
        Long globalWatermark,
        java.util.List<String> timedOutPartitions
) {
}
