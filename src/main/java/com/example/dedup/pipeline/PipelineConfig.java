package com.example.dedup.pipeline;

/**
 * Immutable pipeline configuration.
 *
 * @param maxEntries              bound on retained dedup entries
 * @param retentionMillis         dedup promise horizon beyond the watermark
 * @param maxTombstoneKeys        bound on distinct tombstone keys
 * @param tombstoneTtlMillis      tombstone retention beyond the watermark
 * @param windowSizeMillis        tumbling window size
 * @param windowLatenessMillis    window allowed lateness
 * @param outOfOrdernessMillis    watermark heuristic slack
 * @param autoWatermarkPeriodMillis >0: timer emits watermark from observed
 *                                time each period; 0: manual only
 */
public record PipelineConfig(
        int maxEntries,
        long retentionMillis,
        int maxTombstoneKeys,
        long tombstoneTtlMillis,
        long windowSizeMillis,
        long windowLatenessMillis,
        long outOfOrdernessMillis,
        long autoWatermarkPeriodMillis) {

    public static PipelineConfig defaults() {
        return new PipelineConfig(100_000, 60_000, 100_000, 60_000,
                10_000, 5_000, 5_000, 0);
    }
}
