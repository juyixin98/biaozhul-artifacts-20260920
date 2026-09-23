package com.example.drvb.time;

/**
 * Deterministic {@link Scheduler}: the task fires once per explicit
 * {@link #tick()} (or {@link #tick(int)}) call. No real-time threads, so test
 * runs are exactly reproducible.
 */
public final class ManualScheduler implements Scheduler {

    private Runnable task;
    private int fireCount;

    @Override
    public void start(Runnable task, long periodMillis) {
        if (this.task != null) {
            throw new IllegalStateException("scheduler already started");
        }
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("periodMillis must be positive");
        }
        this.task = task;
    }

    @Override
    public void stop() {
        this.task = null;
    }

    /** Fires the scheduled task once. */
    public void tick() {
        if (task == null) {
            throw new IllegalStateException("scheduler not started");
        }
        fireCount++;
        task.run();
    }

    /** Fires the scheduled task {@code n} times. */
    public void tick(int n) {
        for (int i = 0; i < n; i++) {
            tick();
        }
    }

    public int fireCount() {
        return fireCount;
    }

    public boolean isStarted() {
        return task != null;
    }
}
