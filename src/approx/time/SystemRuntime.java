package approx.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/** Production time/scheduling wiring backed by the system clock and a daemon thread. */
public final class SystemRuntime implements Clock, TaskScheduler {

    private final ScheduledExecutorService executor;

    public SystemRuntime(String threadNamePrefix) {
        AtomicInteger counter = new AtomicInteger();
        ThreadFactory factory = r -> {
            Thread t = new Thread(r, threadNamePrefix + "-" + counter.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
        this.executor = Executors.newSingleThreadScheduledExecutor(factory);
    }

    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }

    @Override
    public CancellationToken schedulePeriodic(long initialDelayMillis, long periodMillis, Runnable task) {
        long first = Math.max(1L, initialDelayMillis);
        long period = Math.max(1L, periodMillis);
        var future = executor.scheduleAtFixedRate(task, first, period, TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
