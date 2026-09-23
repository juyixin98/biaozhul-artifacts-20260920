package com.example.drvb.time;

/** Wall-clock {@link TimeSource} backed by {@link System#currentTimeMillis()}. */
public final class SystemClock implements TimeSource {

    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }
}
