package com.winquant;

/**
 * 手动推进的时间源，用于测试与演示，实现“时间可注入”。
 * 时间只能前进或保持不变，回退会抛出异常。
 */
public final class ManualClock implements Clock {

    private long now;

    public ManualClock(long start) {
        this.now = start;
    }

    @Override
    public synchronized long now() {
        return now;
    }

    /** 将时间推进到 {@code t}。若 {@code t} 小于当前时间则抛出 {@link IllegalArgumentException}。 */
    public synchronized void advanceTo(long t) {
        if (t < now) {
            throw new IllegalArgumentException("clock cannot go backwards: " + t + " < " + now);
        }
        now = t;
    }

    /** 将时间前进 {@code delta} 个单位。 */
    public synchronized void advanceBy(long delta) {
        if (delta < 0) {
            throw new IllegalArgumentException("delta must be >= 0: " + delta);
        }
        now += delta;
    }
}
