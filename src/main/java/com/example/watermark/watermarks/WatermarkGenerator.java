package com.example.watermark.watermarks;

/**
 * Per-partition watermark generator (Flink-style API).
 *
 * <p>{@link #onEvent} is called for every on-time event of the partition;
 * {@link #onPeriodicEmit} is called on each scheduler tick via the output.
 *
 * @param <T> event payload type
 */
public interface WatermarkGenerator<T> {

    /** Called for every on-time event assigned to the partition. */
    void onEvent(StreamEvent event, long currentProcessingTimeMillis);

    /** Called on every periodic emission tick. */
    void onPeriodicEmit(WatermarkOutput output, long currentProcessingTimeMillis);
}
