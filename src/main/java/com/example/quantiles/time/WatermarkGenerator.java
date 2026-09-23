package com.example.quantiles.time;

/**
 * Watermark（水位线）生成器：跟踪已观察到的最大事件时间，并据此声明
 * “事件时间小于等于 {@code watermark} 的数据在按时间序语义上已经到齐”。
 *
 * <p>事件到达时调用 {@link #onEvent(long)}；周期性触发时调用 {@link #onPeriodicEmit()}
 * 取得当前应广播的 watermark。Watermark 单调不减。
 */
public interface WatermarkGenerator {

    /** 观察到一个事件时间戳。 */
    void onEvent(long eventTimestampMillis);

    /**
     * 周期性触发，返回当前 watermark。返回值不会小于历史上返回过的任何值。
     * 初始（尚未见任何事件）返回 {@link Long#MIN_VALUE}。
     */
    long onPeriodicEmit();

    /**
     * 经典的 bounded-out-of-orderness watermark：
     * {@code watermark = maxTimestampSeen - maxOutOfOrdernessMillis}。
     */
    static WatermarkGenerator boundedOutOfOrderness(long maxOutOfOrdernessMillis) {
        if (maxOutOfOrdernessMillis < 0) {
            throw new IllegalArgumentException(
                    "maxOutOfOrdernessMillis 不能为负: " + maxOutOfOrdernessMillis);
        }
        return new WatermarkGenerator() {
            private long maxTimestamp = Long.MIN_VALUE + maxOutOfOrdernessMillis;

            @Override
            public void onEvent(long eventTimestampMillis) {
                maxTimestamp = Math.max(maxTimestamp, eventTimestampMillis);
            }

            @Override
            public long onPeriodicEmit() {
                return maxTimestamp - maxOutOfOrdernessMillis;
            }
        };
    }
}
