package com.example.sessionwindow.time;

/** Wall-clock implementation. */
final class SystemClock implements Clock {

    static final SystemClock INSTANCE = new SystemClock();

    private SystemClock() {
    }

    @Override
    public long currentTimeMillis() {
        return System.currentTimeMillis();
    }
}
