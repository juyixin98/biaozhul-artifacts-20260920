package com.example.quantiles.time;

import java.util.ArrayList;
import java.util.List;

/**
 * 确定性的手动调度器，仅在测试中使用。
 *
 * <p>{@link #advanceMillis(long)} 推进虚拟时间，并按每个任务到期的次数依次触发回调；
 * 任务第一次触发在 {@code now + period}，与 {@link java.util.concurrent.ScheduledExecutorService}
 * 的 {@code scheduleAtFixedRate} 初始延迟=周期的约定一致。
 */
public final class ManualScheduler implements Scheduler {

    private final List<PeriodicTask> tasks = new ArrayList<>();
    private long nowMillis;

    @Override
    public Cancellable schedulePeriodic(long periodMillis, Runnable task) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("periodMillis 必须为正数: " + periodMillis);
        }
        PeriodicTask t = new PeriodicTask(periodMillis, task, nowMillis + periodMillis);
        tasks.add(t);
        return () -> t.cancelled = true;
    }

    /** 推进虚拟时间并触发所有到期任务（同一时刻按注册顺序触发）。 */
    public void advanceMillis(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta 不能为负: " + deltaMillis);
        }
        long target = nowMillis + deltaMillis;
        // 最多触发次数有限，避免 periodMillis 被极端值搞成死循环
        long guard = 0L;
        while (true) {
            PeriodicTask next = null;
            for (PeriodicTask t : tasks) {
                if (!t.cancelled && t.nextFire <= target
                        && (next == null || t.nextFire < next.nextFire)) {
                    next = t;
                }
            }
            if (next == null) {
                break;
            }
            if (++guard > 1_000_000L) {
                throw new IllegalStateException("ManualScheduler 触发次数超过保护上限");
            }
            nowMillis = next.nextFire;
            next.nextFire += next.periodMillis;
            next.task.run();
        }
        nowMillis = target;
    }

    public long nowMillis() {
        return nowMillis;
    }

    /** 立即触发一次所有未取消的任务（不推进时间，不改变后续调度计划）。 */
    public void fireOnceAll() {
        for (PeriodicTask t : tasks) {
            if (!t.cancelled) {
                t.task.run();
            }
        }
    }

    @Override
    public void shutdown() {
        tasks.clear();
    }

    private static final class PeriodicTask {
        final long periodMillis;
        final Runnable task;
        long nextFire;
        boolean cancelled;

        PeriodicTask(long periodMillis, Runnable task, long nextFire) {
            this.periodMillis = periodMillis;
            this.task = task;
            this.nextFire = nextFire;
        }
    }
}
