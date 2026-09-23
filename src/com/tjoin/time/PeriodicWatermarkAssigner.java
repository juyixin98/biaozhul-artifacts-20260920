package com.tjoin.time;

import com.tjoin.core.IntervalJoinOperator;
import com.tjoin.core.StreamSide;
import java.util.function.LongConsumer;

/**
 * 周期性水位线发射：每隔固定处理时间间隔，把某一侧 {@link WatermarkGenerator}
 * 的当前水位线推进连接算子。
 *
 * <p>处理时间来源 {@link ProcessingTimeService} 可注入：
 * 生产环境用 {@link SystemProcessingTimeService}，测试用 {@link ManualProcessingTimeService}
 * 即可在零真实等待下确定性验证“周期性推进水位线”的行为。
 *
 * <p>水位线只进不退：只有生成器给出更大的值时才调用 {@code processWatermark}。
 */
public final class PeriodicWatermarkAssigner implements AutoCloseable {

    private final StreamSide side;
    private final WatermarkGenerator generator;
    private final IntervalJoinOperator operator;
    private final ProcessingTimeService timeService;
    private final long periodMillis;
    private final LongConsumer watermarkObserver; // 可为 null
    private ScheduledTask task;
    private long lastEmitted = IntervalJoinOperator.INITIAL_WATERMARK;

    public PeriodicWatermarkAssigner(StreamSide side,
                                     WatermarkGenerator generator,
                                     IntervalJoinOperator operator,
                                     ProcessingTimeService timeService,
                                     long periodMillis) {
        this(side, generator, operator, timeService, periodMillis, null);
    }

    public PeriodicWatermarkAssigner(StreamSide side,
                                     WatermarkGenerator generator,
                                     IntervalJoinOperator operator,
                                     ProcessingTimeService timeService,
                                     long periodMillis,
                                     LongConsumer watermarkObserver) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("period must be positive");
        }
        this.side = side;
        this.generator = generator;
        this.operator = operator;
        this.timeService = timeService;
        this.periodMillis = periodMillis;
        this.watermarkObserver = watermarkObserver;
    }

    /** 启动周期任务（首次立即发射一次初始水位线，随后按周期触发）。 */
    public void start() {
        if (task != null) {
            throw new IllegalStateException("already started");
        }
        emit();
        task = timeService.scheduleAtFixedRate(periodMillis, periodMillis, this::emit);
    }

    /** 立即取一次生成器水位线并推进算子（非递增则被算子忽略）。 */
    public void emit() {
        long wm = generator.currentWatermark();
        if (wm > lastEmitted) {
            lastEmitted = wm;
            operator.processWatermark(side, wm);
            if (watermarkObserver != null) {
                watermarkObserver.accept(wm);
            }
        }
    }

    public long lastEmittedWatermark() {
        return lastEmitted;
    }

    @Override
    public void close() {
        if (task != null) {
            task.cancel();
            task = null;
        }
    }
}
