package com.example.tjoin.time;

/** Wall-clock {@link Clock} backed by {@link System#currentTimeMillis()}. */
public final class SystemClock implements Clock {

    public static final SystemClock INSTANCE = new SystemClock();

    private SystemClock() {
    }

    @Override
    public long currentTimeMillis() {
        return System.currentTimeMillis();
    }
}
