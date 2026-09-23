package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import com.example.quantiles.time.Clock;
import com.example.quantiles.time.Scheduler;
import com.example.quantiles.time.SystemClock;
import com.example.quantiles.time.WatermarkGenerator;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Consumer;

/**
 * 流式滑动窗口分位数作业：把可注入的 {@link Clock}、{@link Scheduler}、
 * {@link WatermarkGenerator} 与 {@link SlidingWindowQuantileOperator} 组装起来。
 *
 * <p>事件经 {@link #process(Event)} 进入；watermark 周期性由调度器根据当前
 * 事件时间进展生成并推进算子，触发的窗口结果交给构造时给定的回调。
 * 时间与调度均可注入，因此测试里用 MockClock + ManualScheduler 即可确定性重放。
 */
public final class StreamingQuantileJob {

    private final SlidingWindowQuantileOperator operator;
    private final WatermarkGenerator watermarkGenerator;
    private final Consumer<WindowResult> sink;
    private final List<WindowResult> emitted = new CopyOnWriteArrayList<>();

    private Scheduler.Cancellable scheduledTask;
    private volatile boolean started;

    public StreamingQuantileJob(WindowSpec spec,
                                WatermarkGenerator watermarkGenerator,
                                Consumer<WindowResult> sink) {
        this.operator = new SlidingWindowQuantileOperator(spec);
        this.watermarkGenerator = watermarkGenerator;
        this.sink = sink;
    }

    /** 用真实墙钟和给定调度器启动周期性 watermark（periodMillis 为触发周期）。 */
    public void start(Scheduler scheduler, long periodMillis) {
        start(scheduler, new SystemClock(), periodMillis);
    }

    /**
     * 启动作业。clock 仅用于记录/诊断语义；watermark 完全由事件时间生成器决定，
     * 因此即使传入 MockClock 也不影响窗口归属。
     */
    public void start(Scheduler scheduler, Clock clock, long periodMillis) {
        if (started) {
            throw new IllegalStateException("作业已启动");
        }
        started = true;
        this.scheduledTask = scheduler.schedulePeriodic(periodMillis, this::onTimer);
    }

    /** 接收一条事件（乱序在 allowedLateness 容忍范围内可接受）。 */
    public boolean process(Event event) {
        watermarkGenerator.onEvent(event.timestampMillis());
        return operator.process(event);
    }

    /** 一次定时器触发：生成周期 watermark 并推进。可在测试中由 ManualScheduler 驱动。 */
    public void onTimer() {
        long wm = watermarkGenerator.onPeriodicEmit();
        if (wm == Long.MIN_VALUE) {
            return;
        }
        for (WindowResult r : operator.advanceWatermark(wm)) {
            emitted.add(r);
            sink.accept(r);
        }
    }

    /** 停止周期性任务（不释放调度器本身）。 */
    public void stop(Scheduler scheduler) {
        if (scheduledTask != null) {
            scheduledTask.cancel();
            scheduledTask = null;
        }
        started = false;
    }

    public List<WindowResult> emittedResults() {
        return List.copyOf(emitted);
    }

    public long lateDroppedCount() {
        return operator.lateDroppedCount();
    }
}
