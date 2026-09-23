package com.example.quantiles.time;

/**
 * 手动控制的时钟，仅在测试或离线重放中使用。
 * 时间只能向前推进（与真实时钟一致），也可以通过 {@link #setTimeMillis(long)} 直接设定。
 */
public final class MockClock implements Clock {

    private long nowMillis;

    public MockClock() {
        this(0L);
    }

    public MockClock(long startMillis) {
        this.nowMillis = startMillis;
    }

    @Override
    public long nowMillis() {
        return nowMillis;
    }

    public void setTimeMillis(long timeMillis) {
        if (timeMillis < nowMillis) {
            throw new IllegalArgumentException(
                    "MockClock 不允许倒退: current=" + nowMillis + " requested=" + timeMillis);
        }
        this.nowMillis = timeMillis;
    }

    public void advanceMillis(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta 不能为负: " + deltaMillis);
        }
        this.nowMillis += deltaMillis;
    }
}
