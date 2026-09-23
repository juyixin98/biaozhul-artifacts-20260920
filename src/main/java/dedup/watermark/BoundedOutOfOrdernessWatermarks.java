package dedup.watermark;

import dedup.core.Event;

/**
 * 经典“有界乱序”水位线（Flink 风格）：
 * 跟踪观测到的最大事件时间，水位线 = maxEventTime − maxOutOfOrderness。
 *
 * <p>周期触发时才发出新水位线（{@link #onPeriodicTick}）；
 * {@link #onEvent} 只更新内部最大值，避免每个事件都产生水位线抖动。
 *
 * <p>时钟回退安全：旧事件（事件时间小于已见最大值）不会让水位线倒退。
 */
public final class BoundedOutOfOrdernessWatermarks implements WatermarkGenerator {

    private final long maxOutOfOrderness;
    private long maxTimestamp = Long.MIN_VALUE;
    private Long lastEmitted;

    public BoundedOutOfOrdernessWatermarks(long maxOutOfOrderness) {
        if (maxOutOfOrderness < 0) {
            throw new IllegalArgumentException("maxOutOfOrderness 不能为负");
        }
        this.maxOutOfOrderness = maxOutOfOrderness;
    }

    @Override
    public Long onEvent(Event event) {
        // 只取最大值：迟到/回退事件天然不会拉低水位线
        maxTimestamp = Math.max(maxTimestamp, event.eventTime());
        return null;
    }

    @Override
    public Long onPeriodicTick(long currentClockMillis) {
        if (maxTimestamp == Long.MIN_VALUE) {
            return null;
        }
        long candidate = (maxTimestamp < Long.MIN_VALUE + maxOutOfOrderness)
                ? Long.MIN_VALUE
                : maxTimestamp - maxOutOfOrderness; // 饱和减法
        if (lastEmitted != null && candidate <= lastEmitted) {
            return null;
        }
        lastEmitted = candidate;
        return candidate;
    }

    @Override
    public Long currentWatermark() {
        return lastEmitted;
    }

    /** 恢复用：装载内部状态。 */
    public void restore(long maxTimestamp, Long lastEmitted) {
        this.maxTimestamp = maxTimestamp;
        this.lastEmitted = lastEmitted;
    }

    public long maxTimestampValue() { return maxTimestamp; }
}
