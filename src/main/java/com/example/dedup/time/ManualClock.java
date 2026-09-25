package com.example.dedup.time;

/** Deterministic, manually driven clock for tests and deterministic services. */
public final class ManualClock implements Clock {
    private long now;

    public ManualClock() {
        this(0L);
    }

    public ManualClock(long start) {
        this.now = start;
    }

    @Override
    public long currentTimeMillis() {
        return now;
    }

    /** Set absolute time; may move backwards to simulate clock rollback. */
    public void setTime(long t) {
        this.now = t;
    }

    /** Advance time by {@code deltaMillis} (may be negative to roll back). */
    public long advance(long deltaMillis) {
        this.now += deltaMillis;
        return this.now;
    }
}
