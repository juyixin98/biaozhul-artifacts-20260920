package com.example.watermark.watermarks;

/**
 * Bounded-out-of-orderness watermark generator (Flink semantics):
 *
 * <pre>
 *     partition watermark = max(event timestamp seen) - maxOutOfOrderness
 * </pre>
 *
 * Watermarks are emitted periodically, not per event, and only when the
 * computed value advances (the enclosing state clamps too).
 *
 * @param <T> event payload type
 */
public final class BoundedOutOfOrdernessGenerator<T> implements WatermarkGenerator<T> {

    private final long maxOutOfOrderness;
    private long maxTimestamp = Watermark.NO_WATERMARK;

    public BoundedOutOfOrdernessGenerator(long maxOutOfOrdernessMillis) {
        if (maxOutOfOrdernessMillis < 0) {
            throw new IllegalArgumentException(
                    "maxOutOfOrderness must be non-negative: " + maxOutOfOrdernessMillis);
        }
        this.maxOutOfOrderness = maxOutOfOrdernessMillis;
    }

    @Override
    public void onEvent(StreamEvent event, long currentProcessingTimeMillis) {
        maxTimestamp = Math.max(maxTimestamp, event.timestamp());
    }

    @Override
    public void onPeriodicEmit(WatermarkOutput output, long currentProcessingTimeMillis) {
        if (maxTimestamp != Watermark.NO_WATERMARK) {
            output.emitWatermark(maxTimestamp - maxOutOfOrderness);
        }
    }
}
