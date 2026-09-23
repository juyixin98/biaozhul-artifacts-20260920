package com.example.watermark.time;

/**
 * Injectable clock. All watermark timing (idle timeouts, periodic emission)
 * is derived from a clock, so tests can use {@link VirtualClock} and
 * production can use {@link SystemClock}.
 *
 * <p>Time is an epoch millisecond value, matching the event timestamps used
 * throughout the library.
 */
public interface Clock {

    /** Current epoch time in milliseconds. */
    long currentTimeMillis();

    /** Wall clock backed by {@link System#currentTimeMillis()}. */
    static Clock system() {
        return System::currentTimeMillis;
    }
}
