package approxheavy.core;

/**
 * Injectable periodic scheduler. The stream processor seals tumbling windows
 * either when an event forces it or, in production, when a periodic task fires.
 */
public interface Scheduler {
    /** Schedule {@code task} to run every {@code periodMillis}; returns a task id. */
    long scheduleAtFixedRate(long periodMillis, Runnable task);

    void cancel(long id);
}
