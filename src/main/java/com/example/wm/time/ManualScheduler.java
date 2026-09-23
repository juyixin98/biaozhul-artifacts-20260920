package com.example.wm.time;

import java.util.ArrayList;
import java.util.List;

/**
 * 确定性测试调度器：任务不自动执行，测试代码调用 {@link #runDue()}
 * 配合 {@link VirtualClock} 手动触发所有到期任务，从而精确控制
 * 空闲检测、周期推进等时序行为。
 */
public final class ManualScheduler implements Scheduler {

    private static final class Entry {
        final Runnable task;
        long nextRunMs;
        final long periodMs;
        boolean cancelled;

        Entry(Runnable task, long nextRunMs, long periodMs) {
            this.task = task;
            this.nextRunMs = nextRunMs;
            this.periodMs = periodMs;
        }
    }

    private final Clock clock;
    private final List<Entry> entries = new ArrayList<>();
    private boolean closed;

    public ManualScheduler(Clock clock) {
        this.clock = clock;
    }

    @Override
    public synchronized ScheduledTask schedulePeriodic(Runnable task, long delayMs, long periodMs) {
        if (closed) {
            throw new IllegalStateException("scheduler already closed");
        }
        if (periodMs <= 0) {
            throw new IllegalArgumentException("periodMs must be positive: " + periodMs);
        }
        Entry entry = new Entry(task, clock.currentTimeMillis() + Math.max(0, delayMs), periodMs);
        entries.add(entry);
        return new ScheduledTask() {
            @Override
            public void cancel() {
                synchronized (ManualScheduler.this) {
                    entry.cancelled = true;
                }
            }

            @Override
            public boolean isCancelled() {
                synchronized (ManualScheduler.this) {
                    return entry.cancelled;
                }
            }
        };
    }

    /**
     * 按到期顺序执行当前所有已到期的任务；每个任务执行后按其周期排到下一轮。
     * 任务内新调度的任务若已到期也会在本轮被执行（fixed-point 收敛）。
     *
     * @return 本次实际执行的任务次数
     */
    public synchronized int runDue() {
        int ran = 0;
        boolean progress = true;
        while (progress) {
            progress = false;
            Entry due = null;
            long now = clock.currentTimeMillis();
            for (Entry e : entries) {
                if (!e.cancelled && e.nextRunMs <= now && (due == null || e.nextRunMs < due.nextRunMs)) {
                    due = e;
                }
            }
            if (due != null) {
                due.nextRunMs = due.nextRunMs + due.periodMs;
                // 若任务已积压多个周期，丢弃积压，只保留一个未来周期（与 fixed-rate 语义一致）
                if (due.nextRunMs <= now) {
                    due.nextRunMs = now + due.periodMs;
                }
                due.task.run();
                ran++;
                progress = true;
            }
        }
        return ran;
    }

    /** 距离下一个到期任务的毫秒数；没有任务时返回 {@link Long#MAX_VALUE}。 */
    public synchronized long millisUntilNextDue() {
        Long nearest = null;
        long now = clock.currentTimeMillis();
        for (Entry e : entries) {
            if (!e.cancelled) {
                long d = e.nextRunMs - now;
                if (nearest == null || d < nearest) {
                    nearest = d;
                }
            }
        }
        return nearest == null ? Long.MAX_VALUE : Math.max(0, nearest);
    }

    @Override
    public synchronized void close() {
        closed = true;
        entries.forEach(e -> e.cancelled = true);
        entries.clear();
    }
}
