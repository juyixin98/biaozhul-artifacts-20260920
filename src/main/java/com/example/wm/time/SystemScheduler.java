package com.example.wm.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** 基于真实守护线程池的调度器，生产环境使用。 */
public final class SystemScheduler implements Scheduler {
    private final ScheduledExecutorService executor;

    public SystemScheduler() {
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "wm-scheduler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public ScheduledTask schedulePeriodic(Runnable task, long delayMs, long periodMs) {
        if (periodMs <= 0) {
            throw new IllegalArgumentException("periodMs must be positive: " + periodMs);
        }
        var future = executor.scheduleAtFixedRate(
                task, Math.max(0, delayMs), periodMs, TimeUnit.MILLISECONDS);
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
