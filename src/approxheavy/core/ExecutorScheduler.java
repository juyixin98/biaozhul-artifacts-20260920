package approxheavy.core;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** Scheduler backed by a daemon-thread ScheduledExecutorService. */
public final class ExecutorScheduler implements Scheduler, AutoCloseable {
    private final ScheduledExecutorService executor;
    private final AtomicLong nextId = new AtomicLong(1);
    private final java.util.Map<Long, ScheduledFuture<?>> tasks = new java.util.concurrent.ConcurrentHashMap<>();

    public ExecutorScheduler() {
        this.executor = Executors.newScheduledThreadPool(2, r -> {
            Thread t = new Thread(r, "approxheavy-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public long scheduleAtFixedRate(long periodMillis, Runnable task) {
        long id = nextId.getAndIncrement();
        ScheduledFuture<?> future = executor.scheduleAtFixedRate(
                task, periodMillis, periodMillis, TimeUnit.MILLISECONDS);
        tasks.put(id, future);
        return id;
    }

    @Override
    public void cancel(long id) {
        ScheduledFuture<?> future = tasks.remove(id);
        if (future != null) {
            future.cancel(false);
        }
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
