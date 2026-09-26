package com.example.hlc.clock;

/** Physical clock backed by {@link System#currentTimeMillis()}. */
public final class SystemClock implements PhysicalClock {

    @Override
    public long millis() {
        return System.currentTimeMillis();
    }
}
