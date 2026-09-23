package com.example.wm.model;

/**
 * 一条输入事件。
 *
 * @param partition   来源分区（分区/分片/源）标识
 * @param eventTimeMs 事件时间戳（毫秒）
 * @param payload     负载内容（本引擎不解释，仅透传到迟到通道与输出）
 */
public record StreamEvent(String partition, long eventTimeMs, String payload) {
    public StreamEvent {
        if (partition == null || partition.isEmpty()) {
            throw new IllegalArgumentException("partition must be non-empty");
        }
    }
}
