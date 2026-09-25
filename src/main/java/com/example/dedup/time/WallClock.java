package com.example.dedup.time;

/** System wall clock. A backwards jump of the OS clock is observable
 *  through {@link #currentTimeMillis()} (it simply reads the OS); callers
 *  that must be monotone should wrap it in {@link MonotonicClock}. */
public final class WallClock implements Clock {
    @Override
    public long currentTimeMillis() {
        return System.currentTimeMillis();
    }
}
