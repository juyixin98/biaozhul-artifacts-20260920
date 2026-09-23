package com.example.drvb.time;

/**
 * Manually-driven {@link TimeSource} for deterministic tests and reference
 * runs. Time advances only via {@link #advance(long)} or {@link #set(long)}.
 */
public final class SimClock implements TimeSource {

    private long now;

    public SimClock() {
        this(0L);
    }

    public SimClock(long startMillis) {
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
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("cannot move clock backwards");
        }
        this.now += deltaMillis;
    }
}
