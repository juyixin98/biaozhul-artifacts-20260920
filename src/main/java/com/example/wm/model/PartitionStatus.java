package com.example.wm.model;

/** 分区运行状态。 */
public enum PartitionStatus {
    /** 活跃：参与全局水位线的 min 聚合。 */
    ACTIVE,
    /** 空闲：超过 idleTimeoutMs 未收到事件，不参与 min 聚合，不再拖住全局水位线。 */
    IDLE,
    /** 已暂停（显式）：与空闲等价，不参与 min 聚合，但语义上来自用户显式暂停。 */
    PAUSED
}
