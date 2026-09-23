package dedup.time;

import java.util.ArrayList;
import java.util.List;

/**
 * 确定性调度器（测试用）：不创建任何线程。
 * 任务在调用 {@link #runDue()} 时按手动时钟当前时间同步执行，
 * 使“周期触发水位线推进”完全可重放。
 */
public final class DeterministicScheduler implements TaskScheduler {

    private final Clock.ManualClock clock;
    private final List<PeriodicTask> tasks = new ArrayList<>();

    DeterministicScheduler(Clock.ManualClock clock) {
        this.clock = clock;
    }

    @Override
    public Cancellable schedulePeriodic(long delayMillis, long periodMillis, Runnable task) {
        PeriodicTask t = new PeriodicTask(clock.nowMillis() + delayMillis, periodMillis, task);
        tasks.add(t);
        return () -> tasks.remove(t);
    }

    /** 执行所有当前时间点已到期的周期任务；错过的周期按固定速率补跑。 */
    public int runDue() {
        int executed = 0;
        long now = clock.nowMillis();
        for (PeriodicTask t : tasks) {
            while (t.nextRun <= now) {
                t.task.run();
                t.nextRun += t.period;
                executed++;
            }
        }
        return executed;
    }

    /** 推进手动时钟并执行所有因此到期的任务（便捷方法）。 */
    public int advanceAndRun(long deltaMillis) {
        clock.advance(deltaMillis);
        return runDue();
    }

    private static final class PeriodicTask {
        long nextRun;
        final long period;
        final Runnable task;

        PeriodicTask(long nextRun, long period, Runnable task) {
            this.nextRun = nextRun;
            this.period = period;
            this.task = task;
        }
    }
}
