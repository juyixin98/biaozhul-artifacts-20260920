package com.example.drvb.time;

/**
 * Injectable processing-time clock. Production code uses
 * {@link SystemClock}; tests and deterministic demos use {@link SimClock}.
 */
public interface TimeSource {

    /** Current processing time, epoch milliseconds. */
    long nowMillis();
}
