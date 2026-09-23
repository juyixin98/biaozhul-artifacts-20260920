package com.example.watermark.watermarks;

import java.util.function.Supplier;

/**
 * Strategy bundling generator creation with timing configuration.
 *
 * @param <T> event payload type
 */
public record WatermarkStrategy<T>(
        Supplier<WatermarkGenerator<T>> generatorSupplier,
        WatermarkConfig config) {

    /**
     * Bounded out-of-orderness with the given bound; a generator instance is
     * created per partition.
     */
    public static <T> WatermarkStrategy<T> boundedOutOfOrderness(
            long maxOutOfOrdernessMillis, WatermarkConfig config) {
        return new WatermarkStrategy<>(
                () -> new BoundedOutOfOrdernessGenerator<>(maxOutOfOrdernessMillis), config);
    }

    /** {@code maxOutOfOrderness == 0}: watermark tracks the max timestamp seen. */
    public static <T> WatermarkStrategy<T> forMonotonousTimestamps(WatermarkConfig config) {
        return boundedOutOfOrderness(0L, config);
    }
}
