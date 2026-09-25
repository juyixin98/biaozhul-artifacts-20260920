package com.example.tjoin.time;

/**
 * Injectable processing-time scheduler. Used by the operator to detect
 * stalled (idle) sides on periodic processing-time ticks rather than on
 * wall-clock threads.
 */
public interface Scheduler {

    /** Run {@code action} once when processing time reaches {@code atMillis}. */
    void scheduleAt(long atMillis, Runnable action);

    /** Run {@code action} once after {@code delayMillis} of processing time. */
    void scheduleAfter(long delayMillis, Runnable action);
}
