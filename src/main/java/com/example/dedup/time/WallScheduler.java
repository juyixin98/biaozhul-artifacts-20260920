package com.example.dedup.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** Real processing-time scheduler backed by a daemon ScheduledExecutorService. */
public final class WallScheduler implements Scheduler, AutoCloseable {
    private final ScheduledExecutorService exec;

    public WallScheduler() {
        this.exec = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "dedup-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public Cancellable schedulePeriodic(long periodMillis, Runnable task) {
        var future = exec.scheduleAtFixedRate(task, periodMillis, periodMillis, TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public void close() {
        exec.shutdownNow();
    }
}
