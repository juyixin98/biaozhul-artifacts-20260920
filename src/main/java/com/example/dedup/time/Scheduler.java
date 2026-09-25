package com.example.dedup.time;

/**
 * Injectable processing-time scheduler. Used by the pipeline for periodic
 * watermark emission (auto mode). Implementations must be safe to use with
 * backward jumps of the underlying clock without throwing.
 */
public interface Scheduler {
    /** Schedule a periodic task; returns a handle that can cancel it. */
    Cancellable schedulePeriodic(long periodMillis, Runnable task);

    /** A scheduler that does nothing (manual mode): nothing ever fires on its own. */
    static Scheduler noop() {
        return new Scheduler() {
            @Override
            public Cancellable schedulePeriodic(long periodMillis, Runnable task) {
                return () -> {
                };
            }
        };
    }

    interface Cancellable {
        void cancel();
    }
}
