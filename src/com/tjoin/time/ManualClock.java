package com.tjoin.time;

/**
 * 手动时钟：时间只在调用 {@link #advanceBy(long)} / {@link #setTime(long)} 时前进。
 */
public final class ManualClock implements Clock {

    private long now;

    public ManualClock(long startMillis) {
        this.now = startMillis;
    }

    @Override
    public long now() {
        return now;
    }

    public void setTime(long millis) {
        this.now = millis;
    }

    public void advanceBy(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("time cannot move backwards");
        }
        this.now += deltaMillis;
    }
}
