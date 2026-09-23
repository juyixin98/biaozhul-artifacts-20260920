package com.example.drvb.stream;

import com.example.drvb.core.RuleRegistry;

/**
 * Event-time watermark tracker.
 *
 * <p>The watermark after observing a set of events is
 * {@code maxEventTime - delayMillis}: the system's promise that no event with
 * a time strictly below it is expected to arrive on time. Events at or above
 * the watermark are on time; events below it are late but accepted up to the
 * registry's lateness horizon.
 *
 * <p>Before any event is seen the watermark is {@link Long#MIN_VALUE}
 * ("nothing observed"), so the first rule publication is never blocked by a
 * stale watermark.
 */
public final class WatermarkTracker {

    private final long delayMillis;
    private long maxEventTime = Long.MIN_VALUE;
    private long watermark = Long.MIN_VALUE;

    public WatermarkTracker(long delayMillis) {
        if (delayMillis < 0) {
            throw new IllegalArgumentException("delayMillis must be >= 0");
        }
        this.delayMillis = delayMillis;
    }

    /** Observes an event's time and advances the watermark. */
    public long observe(long eventTime) {
        if (eventTime > maxEventTime) {
            maxEventTime = eventTime;
            long candidate = RuleRegistry.saturatingSubtract(maxEventTime, delayMillis);
            if (candidate > watermark) {
                watermark = candidate;
            }
        }
        return watermark;
    }

    /**
     * Explicitly advances the watermark (e.g. an idle-time timer signal or an
     * admin call). Never moves it backwards.
     */
    public long advanceTo(long newWatermark) {
        if (newWatermark > watermark) {
            watermark = newWatermark;
            if (watermark > maxEventTime) {
                maxEventTime = watermark;
            }
        }
        return watermark;
    }

    public long watermark() {
        return watermark;
    }

    public long maxEventTime() {
        return maxEventTime;
    }

    public long delayMillis() {
        return delayMillis;
    }
}
