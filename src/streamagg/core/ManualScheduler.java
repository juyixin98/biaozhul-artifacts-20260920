package streamagg.core;

import java.util.ArrayList;
import java.util.List;

/**
 * 手动调度器：任务被记录下来但不会自动执行，由测试调用
 * {@link #tick()} / {@link #tick(long)} 推进虚拟时钟并触发到期任务。
 *
 * <p>语义对齐 scheduleAtFixedRate：若多次 tick 跨越了多个周期，任务只补跑一次，
 * 下一次到期时间对齐到固定周期网格。
 */
public final class ManualScheduler implements Scheduler {
    private static final class Task {
        final Runnable body;
        long nextDueAt;
        final long period;
        boolean cancelled;

        Task(Runnable body, long nextDueAt, long period) {
            this.body = body;
            this.nextDueAt = nextDueAt;
            this.period = period;
        }
    }

    private final List<Task> tasks = new ArrayList<>();
    private long now = 0;

    @Override
    public Cancellable scheduleAtFixedRate(Runnable task, long initialDelayMillis, long periodMillis) {
        Task t = new Task(task, now + initialDelayMillis, periodMillis);
        tasks.add(t);
        return () -> t.cancelled = true;
    }

    /** 虚拟时钟前进 {@code millis}，触发期间到期的任务。 */
    public void tick(long millis) {
        now += millis;
        for (Task t : tasks) {
            if (t.cancelled) {
                continue;
            }
            if (now >= t.nextDueAt) {
                t.body.run();
                // 对齐到周期网格
                while (t.nextDueAt <= now) {
                    t.nextDueAt += t.period;
                }
            }
        }
    }

    /** 触发一次所有已到期任务（不推进时钟）。 */
    public void tick() {
        tick(0);
    }

    public long virtualNow() {
        return now;
    }
}
