package com.example.watermark.time;

/**
 * Injectable scheduler, mirroring {@link Clock}. The library schedules one
 * periodic task (the watermark emission / idle-detection tick) through this
 * interface, so all timing can be driven deterministically by a
 * {@link VirtualClock} in tests.
 */
public interface Scheduler {

    /**
     * Schedule {@code task} to run periodically every {@code periodMillis},
     * starting at {@code now + initialDelayMillis} (clock time).
     */
    ScheduledTask schedulePeriodically(Runnable task, long initialDelayMillis, long periodMillis);
}
