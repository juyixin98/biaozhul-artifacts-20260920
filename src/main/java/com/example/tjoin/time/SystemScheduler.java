package com.example.tjoin.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Production {@link Scheduler}: timers fire on a real scheduled thread pool,
 * stamped with a {@link SystemClock}. Used by the JSON service unless a
 * manual clock is requested.
 */
public final class SystemScheduler implements Scheduler, AutoCloseable {

    private final ScheduledExecutorService executor;
    private final Clock clock;

    public SystemScheduler() {
        this(ClockHolder.CLOCK, "tjoin-scheduler");
    }

    public SystemScheduler(Clock clock, String threadName) {
        this.clock = clock;
        this.executor = Executors.newScheduledThreadPool(1, r -> {
            Thread t = new Thread(r, threadName);
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public void scheduleAt(long atMillis, Runnable action) {
        long delay = Math.max(0L, atMillis - clock.currentTimeMillis());
        ScheduledFuture<?> ignored = executor.schedule(action, delay, TimeUnit.MILLISECONDS);
    }

    @Override
    public void scheduleAfter(long delayMillis, Runnable action) {
        ScheduledFuture<?> ignored = executor.schedule(action, Math.max(0L, delayMillis),
                TimeUnit.MILLISECONDS);
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }

    private static final class ClockHolder {
        static final Clock CLOCK = SystemClock.INSTANCE;
    }
}
