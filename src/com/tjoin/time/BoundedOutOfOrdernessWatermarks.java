package com.tjoin.time;

import com.tjoin.core.Event;
import com.tjoin.core.IntervalJoinOperator;

/**
 * 有界乱序水位线生成器。
 *
 * <p>维护观测到的最大事件时间戳 {@code maxTs}，水位线为
 * <pre>
 *     watermark = maxTs - maxOutOfOrderness
 * </pre>
 * （迟到定义：{@code event.ts < watermark}）。{@code maxOutOfOrderness = 0}
 * 表示严格递增时间戳流。尚未观测到事件时水位线为 {@link Long#MIN_VALUE}。
 */
public final class BoundedOutOfOrdernessWatermarks implements WatermarkGenerator {

    private final long maxOutOfOrderness;
    private long maxTimestamp = Long.MIN_VALUE;

    public BoundedOutOfOrdernessWatermarks(long maxOutOfOrderness) {
        if (maxOutOfOrderness < 0) {
            throw new IllegalArgumentException("maxOutOfOrderness must be >= 0");
        }
        this.maxOutOfOrderness = maxOutOfOrderness;
    }

    @Override
    public void onEvent(Event event) {
        maxTimestamp = Math.max(maxTimestamp, event.timestamp());
    }

    @Override
    public long currentWatermark() {
        if (maxTimestamp == Long.MIN_VALUE) {
            return IntervalJoinOperator.INITIAL_WATERMARK;
        }
        return maxTimestamp - maxOutOfOrderness;
    }
}
