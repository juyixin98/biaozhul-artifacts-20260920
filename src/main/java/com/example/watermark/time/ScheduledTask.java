package com.example.watermark.time;

/**
 * Handle for a scheduled, possibly periodic task.
 */
public interface ScheduledTask {

    /** Cancel the task; a cancelled task never fires again. */
    void cancel();

    /** Whether {@link #cancel()} has been called. */
    boolean isCancelled();
}
