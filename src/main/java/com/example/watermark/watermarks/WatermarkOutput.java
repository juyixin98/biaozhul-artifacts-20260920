package com.example.watermark.watermarks;

/**
 * Sink for watermark emissions from a {@link WatermarkGenerator}.
 */
@FunctionalInterface
public interface WatermarkOutput {

    /** Emit a watermark. Implementations must clamp to monotonic progress. */
    void emitWatermark(long watermark);
}
