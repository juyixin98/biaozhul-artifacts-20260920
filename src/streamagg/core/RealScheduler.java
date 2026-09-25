package streamagg.core;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** 基于 ScheduledExecutorService 的真实调度器。 */
public final class RealScheduler implements Scheduler, AutoCloseable {
    private final ScheduledExecutorService executor;

    public RealScheduler() {
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "streamagg-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public Cancellable scheduleAtFixedRate(Runnable task, long initialDelayMillis, long periodMillis) {
        var future = executor.scheduleAtFixedRate(task, initialDelayMillis, periodMillis, TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
