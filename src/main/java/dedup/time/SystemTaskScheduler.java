package dedup.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** 基于单线程 ScheduledExecutorService 的生产调度器，线程为守护线程。 */
final class SystemTaskScheduler implements TaskScheduler {

    private final Clock clock;
    private final ScheduledExecutorService executor;

    SystemTaskScheduler(Clock clock) {
        this.clock = clock;
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "dedup-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public Cancellable schedulePeriodic(long delayMillis, long periodMillis, Runnable task) {
        var future = executor.scheduleAtFixedRate(
                task, delayMillis, periodMillis, TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    // clock 仅用于语义完整性（任务触发由 executor 计时）
    @SuppressWarnings("unused")
    private Clock clock() { return clock; }
}
