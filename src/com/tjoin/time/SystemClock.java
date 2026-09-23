package com.tjoin.time;

/** 基于系统墙钟的时钟。 */
final class SystemClock implements Clock {

    static final SystemClock INSTANCE = new SystemClock();

    private SystemClock() {
    }

    @Override
    public long now() {
        return System.currentTimeMillis();
    }
}
