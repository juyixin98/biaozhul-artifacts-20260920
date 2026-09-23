package com.example.watermark.time;

/**
 * Deterministic clock whose time only moves when {@link #advanceTo(long)} or
 * {@link #advanceBy(long)} is called. Every listener registered with
 * {@link #addTickListener(Runnable)} is notified (in registration order)
 * after each advance; deterministic schedulers hook in here to fire timers.
 */
public final class VirtualClock implements Clock {

    private long currentTime;
    private final java.util.List<Runnable> tickListeners = new java.util.ArrayList<>();

    public VirtualClock() {
        this(0L);
    }

    public VirtualClock(long startTimeMillis) {
        this.currentTime = startTimeMillis;
    }

    @Override
    public long currentTimeMillis() {
        return currentTime;
    }

    /** Register a listener invoked after every advance. */
    public void addTickListener(Runnable listener) {
        tickListeners.add(listener);
    }

    /**
     * Move the clock to {@code targetTimeMillis}. Time cannot go backwards:
     * targets at or before the current time are a no-op.
     */
    public void advanceTo(long targetTimeMillis) {
        if (targetTimeMillis <= currentTime) {
            return;
        }
        currentTime = targetTimeMillis;
        fireTick();
    }

    /** Move the clock forward by {@code durationMillis} (must be non-negative). */
    public void advanceBy(long durationMillis) {
        if (durationMillis < 0) {
            throw new IllegalArgumentException("duration must be non-negative: " + durationMillis);
        }
        currentTime += durationMillis;
        fireTick();
    }

    private void fireTick() {
        // Copy to tolerate listeners that (un)register during dispatch.
        for (Runnable listener : new java.util.ArrayList<>(tickListeners)) {
            listener.run();
        }
    }
}
