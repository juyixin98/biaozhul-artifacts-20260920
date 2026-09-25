package streamagg.time;

import java.time.Duration;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** Scheduler backed by a daemon-thread {@link ScheduledExecutorService}. */
public final class SystemScheduler implements Scheduler, AutoCloseable {

    private final ScheduledExecutorService executor;

    public SystemScheduler() {
        this.executor = Executors.newScheduledThreadPool(2, r -> {
            Thread t = new Thread(r, "streamagg-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public ScheduledTask schedule(Duration delay, Runnable task) {
        var future = executor.schedule(task, delay.toMillis(), TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public ScheduledTask scheduleAtFixedRate(Duration initialDelay, Duration period, Runnable task) {
        var future = executor.scheduleAtFixedRate(task, initialDelay.toMillis(), period.toMillis(),
                TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
