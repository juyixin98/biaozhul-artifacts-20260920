package com.example.watermark.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Real-time scheduler backed by a single-thread {@link ScheduledExecutorService}.
 * Pair with {@link Clock#system()} for a live (non-deterministic) run; the
 * library and JSON service are otherwise identical.
 *
 * <p>This scheduler owns its executor thread and must be {@link #close()}d.
 */
public final class ExecutorScheduler implements Scheduler, AutoCloseable {

    private final ScheduledExecutorService executor =
            Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "watermark-scheduler");
                t.setDaemon(true);
                return t;
            });

    @Override
    public ScheduledTask schedulePeriodically(Runnable task, long initialDelayMillis, long periodMillis) {
        ScheduledFuture<?> future = executor.scheduleAtFixedRate(
                task, initialDelayMillis, periodMillis, TimeUnit.MILLISECONDS);
        return new ScheduledTask() {
            @Override
            public void cancel() {
                future.cancel(false);
            }

            @Override
            public boolean isCancelled() {
                return future.isCancelled();
            }
        };
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
