package com.example.cptx.core;

/**
 * 测试用可变时钟：时间只能通过 advance/set 显式推进，
 * 用于确定性地驱动按时间触发的检查点策略。
 */
public final class MutableClock implements Clock {
    private long now;

    public MutableClock() {
        this(0L);
    }

    public MutableClock(long startMillis) {
        this.now = startMillis;
    }

    @Override
    public long nowMillis() {
        return now;
    }

    public void set(long millis) {
        this.now = millis;
    }

    public void advance(long deltaMillis) {
        if (deltaMillis < 0) throw new IllegalArgumentException("时间不能倒流");
        this.now += deltaMillis;
    }
}
