package com.example.tjoin.watermark;

/**
 * Per-side, injectable watermark generator implementing the standard
 * bounded-out-of-orderness strategy:
 *
 * <pre>
 *     watermark = maxObservedEventTime - maxOutOfOrderness
 * </pre>
 *
 * <p>It is purely event-driven ({@link #onEvent(long, long)} recomputes on
 * every event) and additionally tracks processing-time idleness: callers
 * register an {@link IdleListener} and the generator marks the side idle
 * when no event has been seen for the configured timeout, active again on
 * the next event. No wall-clock thread is used — idleness is evaluated by
 * {@link #maybeCheckIdle(long)} from a processing-time tick driven by the
 * injectable scheduler.</p>
 *
 * <p>Thread-safety: all methods are {@code synchronized}; the join operator
 * serializes access anyway, but this keeps the generator safe to probe from
 * a status endpoint.</p>
 */
public final class WatermarkGenerator {

    /** Notified when a side transitions between idle and active. */
    public interface IdleListener {
        void onIdleStateChange(boolean idle, long atProcessingTime);
    }

    private final String sideLabel;
    private final long maxOutOfOrderness;
    private final long idleTimeoutMillis;

    private long maxEventTime = Long.MIN_VALUE;
    private long currentWatermark = Long.MIN_VALUE;
    private long lastActivityProcessingTime = Long.MIN_VALUE;
    private boolean initialized;
    private boolean idle;
    private IdleListener idleListener;

    public WatermarkGenerator(String sideLabel, long maxOutOfOrderness, long idleTimeoutMillis) {
        if (maxOutOfOrderness < 0) {
            throw new IllegalArgumentException("maxOutOfOrderness must be >= 0");
        }
        this.sideLabel = sideLabel;
        this.maxOutOfOrderness = maxOutOfOrderness;
        this.idleTimeoutMillis = idleTimeoutMillis;
    }

    public void setIdleListener(IdleListener listener) {
        this.idleListener = listener;
    }

    /**
     * Observe an event with the given event time at the given processing
     * time. Returns the new watermark (which may be unchanged).
     */
    public synchronized long onEvent(long eventTime, long processingTime) {
        initialized = true;
        lastActivityProcessingTime = processingTime;
        if (idle) {
            setIdle(false, processingTime);
        }
        if (eventTime > maxEventTime) {
            maxEventTime = eventTime;
            long wm = maxEventTime - maxOutOfOrderness;
            if (wm > currentWatermark) {
                currentWatermark = wm;
            }
        }
        return currentWatermark;
    }

    /**
     * Explicitly set the watermark (e.g. an injected WATERMARK element from
     * the service). Only advances it.
     */
    public synchronized long onWatermark(long watermark, long processingTime) {
        initialized = true;
        lastActivityProcessingTime = processingTime;
        if (idle) {
            setIdle(false, processingTime);
        }
        if (watermark > currentWatermark) {
            currentWatermark = watermark;
            if (watermark + maxOutOfOrderness > maxEventTime) {
                maxEventTime = watermark + maxOutOfOrderness;
            }
        }
        return currentWatermark;
    }

    /**
     * Processing-time tick: mark the side idle if no event has arrived
     * within {@code idleTimeoutMillis} (when configured &gt; 0).
     *
     * @return true if the idle flag changed during this check
     */
    public synchronized boolean maybeCheckIdle(long processingTime) {
        if (idleTimeoutMillis <= 0 || !initialized || idle) {
            return false;
        }
        if (lastActivityProcessingTime == Long.MIN_VALUE) {
            return false;
        }
        if (processingTime - lastActivityProcessingTime >= idleTimeoutMillis) {
            setIdle(true, processingTime);
            return true;
        }
        return false;
    }

    private void setIdle(boolean nowIdle, long processingTime) {
        if (this.idle == nowIdle) {
            return;
        }
        this.idle = nowIdle;
        if (idleListener != null) {
            idleListener.onIdleStateChange(nowIdle, processingTime);
        }
    }

    public synchronized long currentWatermark() {
        return currentWatermark;
    }

    public synchronized boolean isIdle() {
        return idle;
    }

    public synchronized long maxObservedEventTime() {
        return maxEventTime;
    }

    public synchronized boolean isInitialized() {
        return initialized;
    }

    @Override
    public synchronized String toString() {
        return sideLabel + "WatermarkGenerator{wm=" + currentWatermark
                + (idle ? ", IDLE" : ", active") + "}";
    }
}
