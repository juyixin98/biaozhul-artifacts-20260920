package com.example.watermark.watermarks;

/**
 * Timing configuration for watermark emission and idle detection.
 *
 * @param autoWatermarkIntervalMillis periodic tick interval (emission + idle checks)
 * @param idleTimeoutMillis           partition is declared idle when no event
 *                                    has arrived for this long (processing time);
 *                                    a non-positive value disables idle detection
 * @param allowedLatenessMillis       informational: events with
 *                                    {@code timestamp <= globalWatermark} are late
 *                                    regardless; kept for strategy documentation
 */
public record WatermarkConfig(
        long autoWatermarkIntervalMillis,
        long idleTimeoutMillis,
        long allowedLatenessMillis) {

    public WatermarkConfig {
        if (autoWatermarkIntervalMillis <= 0) {
            throw new IllegalArgumentException(
                    "autoWatermarkInterval must be positive: " + autoWatermarkIntervalMillis);
        }
        if (allowedLatenessMillis < 0) {
            throw new IllegalArgumentException(
                    "allowedLateness must be non-negative: " + allowedLatenessMillis);
        }
    }

    public static final long DEFAULT_EMIT_INTERVAL = 100L;

    /** Defaults: 100ms ticks, 500ms idle timeout, no extra allowed lateness. */
    public static WatermarkConfig defaults() {
        return new WatermarkConfig(DEFAULT_EMIT_INTERVAL, 500L, 0L);
    }

    public static WatermarkConfig of(long interval, long idleTimeout) {
        return new WatermarkConfig(interval, idleTimeout, 0L);
    }
}
