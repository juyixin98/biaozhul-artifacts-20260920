package com.example.wm.model;

/**
 * 事件的处理分类。本引擎的约定：
 * 全局水位线为 w 时，断言“事件时间 ≤ w 的事件都已到齐”，
 * 因此事件时间戳 t 满足 {@code t <= w} 的事件进入迟到通道（边界含等于）。
 */
public enum Classification {
    /** 准时：eventTimeMs 严格大于当前全局水位线。 */
    ON_TIME,
    /** 迟到：eventTimeMs 小于等于当前全局水位线（含恢复分区重放的旧事件）。 */
    LATE
}
