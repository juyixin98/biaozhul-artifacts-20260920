package com.example.wm.model;

/**
 * 水位线协调参数。
 *
 * @param outOfOrdernessBoundMs 单分区最大乱序等待宽度 B：
 *                              分区水位线 = 该分区已见最大事件时间 − B
 * @param idleTimeoutMs         空闲超时：分区自上次收到事件起，经过该时长仍无新事件，
 *                              则在下次 tick/事件到达时被标记为空闲（边界含等于）
 */
public record WatermarkConfig(long outOfOrdernessBoundMs, long idleTimeoutMs) {

    public static final long NO_IDLE_TIMEOUT = Long.MAX_VALUE;

    public WatermarkConfig {
        if (outOfOrdernessBoundMs < 0) {
            throw new IllegalArgumentException("outOfOrdernessBoundMs must be >= 0");
        }
        if (idleTimeoutMs <= 0) {
            throw new IllegalArgumentException("idleTimeoutMs must be > 0");
        }
    }

    public static WatermarkConfig of(long boundMs, long idleTimeoutMs) {
        return new WatermarkConfig(boundMs, idleTimeoutMs);
    }
}
