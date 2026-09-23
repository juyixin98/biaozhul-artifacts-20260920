package com.example.sessionwindow.engine;

import com.example.sessionwindow.model.Event;

/**
 * Punctuated watermark generator. Tracks the maximum event timestamp seen and
 * proposes {@code maxTimestamp - outOfOrderness} as the watermark. It never
 * proposes a watermark lower than one already emitted.
 *
 * <p>Used by the stateful service when explicit watermarks are not supplied
 * per event; the stateless batch endpoint takes watermarks explicitly.</p>
 */
public final class WatermarkGenerator {

    private final long outOfOrderness;
    private long maxTimestamp = Long.MIN_VALUE;
    private long lastWatermark = Long.MIN_VALUE;

    public WatermarkGenerator(long outOfOrderness) {
        if (outOfOrderness < 0) {
            throw new IllegalArgumentException("outOfOrderness must be >= 0");
        }
        this.outOfOrderness = outOfOrderness;
    }

    /**
     * Observe an event.
     *
     * @return the new watermark if it advanced, otherwise {@link Long#MIN_VALUE}
     *         meaning "no change"
     */
    public long onEvent(Event event) {
        long ts = event.timestamp();
        if (ts <= maxTimestamp) {
            return Long.MIN_VALUE;
        }
        maxTimestamp = ts;
        long candidate = maxTimestamp - outOfOrderness;
        if (candidate > lastWatermark) {
            lastWatermark = candidate;
            return candidate;
        }
        return Long.MIN_VALUE;
    }

    public long currentWatermark() {
        return lastWatermark;
    }
}
