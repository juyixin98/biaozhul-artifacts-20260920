package com.example.hlc.clock;

/**
 * Deterministic, manually advanced clock for tests and demos.
 * Can be moved backwards to simulate physical clock rollback (NTP step,
 * VM suspend/resume, manual date changes).
 */
public final class VirtualClock implements PhysicalClock {

    private long millis;

    public VirtualClock(long initialMillis) {
        this.millis = initialMillis;
    }

    @Override
    public synchronized long millis() {
        return millis;
    }

    public synchronized void set(long newMillis) {
        this.millis = newMillis;
    }

    public synchronized void advance(long deltaMillis) {
        this.millis += deltaMillis;
    }

    /** Moves the clock backwards; used to prove HLC monotonicity survives rollback. */
    public synchronized void rewind(long deltaMillis) {
        this.millis -= deltaMillis;
    }
}
