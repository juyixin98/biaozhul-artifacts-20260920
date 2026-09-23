package com.example.drvb.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** {@link Scheduler} backed by a single-thread daemon scheduled executor. */
public final class WallScheduler implements Scheduler {

    private ScheduledExecutorService executor;

    @Override
    public synchronized void start(Runnable task, long periodMillis) {
        if (executor != null) {
            throw new IllegalStateException("scheduler already started");
        }
        executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "drvb-scheduler");
            t.setDaemon(true);
            return t;
        });
        executor.scheduleAtFixedRate(task, periodMillis, periodMillis,
                TimeUnit.MILLISECONDS);
    }

    @Override
    public synchronized void stop() {
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
    }
}
