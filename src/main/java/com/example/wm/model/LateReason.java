package com.example.wm.model;

/** 迟到原因。 */
public enum LateReason {
    /** 普通迟到：事件时间戳不大于当前全局水位线。 */
    NORMAL,
    /** 恢复旧事件：分区从 IDLE/PAUSED 重新接入后重放的旧事件。 */
    RECOVERED_OLD
}
