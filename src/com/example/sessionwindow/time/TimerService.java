package com.example.sessionwindow.time;

/**
 * Injectable scheduler. Lets the service periodically inject watermarks
 * without owning any thread itself.
 */
public interface TimerService {

    /** Run {@code task} once after {@code delayMillis} of processing time. */
    TimerHandle scheduleOnce(long delayMillis, Runnable task);

    /** Run {@code task} periodically, first after {@code delayMillis}. */
    TimerHandle scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task);

    void shutdown();
}
