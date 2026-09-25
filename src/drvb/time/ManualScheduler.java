package drvb.time;

import java.util.ArrayList;
import java.util.List;

/**
 * 确定性调度器：不创建任何线程，任务只在调用 {@link #runDue()} 时才执行。
 *
 * <p>每次调用时，对所有已注册任务按自注册以来经过的完整周期数做"补跑"
 * （catch-up 语义），因此把 {@link ManualClock} 推进 N 个周期后调用一次，
 * 任务恰好触发 N 次。任务抛出的异常被记录在句柄上而不中断其他任务。
 */
public final class ManualScheduler implements Scheduler {

    private final Clock clock;
    private final List<ManualTask> tasks = new ArrayList<>();
    private boolean closed;

    public ManualScheduler(Clock clock) {
        this.clock = clock;
    }

    @Override
    public synchronized ScheduledTask scheduleFixedRate(String name, long periodMillis, Runnable task) {
        if (closed) {
            throw new IllegalStateException("调度器已关闭");
        }
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("periodMillis 必须为正数");
        }
        ManualTask t = new ManualTask(name, periodMillis, clock.nowMillis(), task);
        tasks.add(t);
        return t;
    }

    /**
     * 执行所有到点的周期任务（含补跑）。返回本次实际执行的任务名（按执行顺序，可重复）。
     */
    public synchronized List<String> runDue() {
        long now = clock.nowMillis();
        List<String> fired = new ArrayList<>();
        for (ManualTask t : tasks) {
            if (t.cancelled) {
                continue;
            }
            long expected = Math.max(0L, (now - t.registeredAt) / t.periodMillis);
            while (t.runCount < expected) {
                t.runCount++;
                fired.add(t.name);
                try {
                    t.task.run();
                } catch (RuntimeException e) {
                    t.lastError = e;
                }
            }
        }
        return fired;
    }

    /** 当前任务快照（用于管理接口）。 */
    @Override
    public synchronized List<ScheduledTask> tasks() {
        return new ArrayList<>(tasks);
    }

    @Override
    public synchronized void close() {
        closed = true;
        for (ManualTask t : tasks) {
            t.cancelled = true;
        }
    }

    static final class ManualTask implements ScheduledTask {
        final String name;
        final long periodMillis;
        final long registeredAt;
        final Runnable task;
        volatile long runCount;
        volatile boolean cancelled;
        volatile RuntimeException lastError;

        ManualTask(String name, long periodMillis, long registeredAt, Runnable task) {
            this.name = name;
            this.periodMillis = periodMillis;
            this.registeredAt = registeredAt;
            this.task = task;
        }

        @Override
        public String name() {
            return name;
        }

        @Override
        public long periodMillis() {
            return periodMillis;
        }

        @Override
        public void cancel() {
            cancelled = true;
        }

        @Override
        public long runCount() {
            return runCount;
        }

        long nextRunMillis() {
            return registeredAt + (runCount + 1) * periodMillis;
        }
    }
}
