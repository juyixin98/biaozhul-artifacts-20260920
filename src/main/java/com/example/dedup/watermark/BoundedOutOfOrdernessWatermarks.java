package com.example.dedup.watermark;

/**
 * Heuristic watermark for bounded out-of-orderness:
 *
 *   watermark = maxObservedEventTime - maxOutOfOrdernessMillis
 *
 * Explicit advances ({@link #tryAdvance}) are remembered and the reported
 * watermark is the max of the heuristic and the explicit value, so a timer
 * may push progress but a late explicit push cannot move it backwards.
 *
 * An event is "late" when eventTime < watermark.
 */
public final class BoundedOutOfOrdernessWatermarks implements WatermarkGenerator {

    private final long maxOutOfOrdernessMillis;
    private long maxObserved = Long.MIN_VALUE;
    private long explicit = Long.MIN_VALUE;

    public BoundedOutOfOrdernessWatermarks(long maxOutOfOrdernessMillis) {
        if (maxOutOfOrdernessMillis < 0) {
            throw new IllegalArgumentException("maxOutOfOrdernessMillis must be >= 0");
        }
        this.maxOutOfOrdernessMillis = maxOutOfOrdernessMillis;
    }

    @Override
    public void observe(long eventTime) {
        if (eventTime > maxObserved) {
            maxObserved = eventTime;
        }
    }

    @Override
    public long watermark() {
        return Math.max(heuristicWatermark(), explicit);
    }

    @Override
    public long heuristicWatermark() {
        return (maxObserved == Long.MIN_VALUE)
                ? Long.MIN_VALUE
                : maxObserved - maxOutOfOrdernessMillis;
    }

    @Override
    public boolean tryAdvance(long target) {
        if (target <= watermark()) {
            return false;
        }
        explicit = target;
        return true;
    }

    @Override
    public void restore(long watermark, long maxObserved) {
        this.explicit = watermark;
        this.maxObserved = maxObserved;
    }

    public long maxObserved() {
        return maxObserved;
    }
}
