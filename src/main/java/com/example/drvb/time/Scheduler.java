package com.example.drvb.time;

/**
 * Injectable periodic scheduler. The engine registers one maintenance task
 * (watermark advancement + historical-version garbage collection + result
 * purging); production wiring drives it with {@link WallScheduler}, tests with
 * {@link ManualScheduler}.
 */
public interface Scheduler {

    /** Starts periodic delivery of the given task. */
    void start(Runnable task, long periodMillis);

    /** Stops delivery; a no-op when not started. */
    void stop();
}
