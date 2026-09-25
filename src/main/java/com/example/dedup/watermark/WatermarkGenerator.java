package com.example.dedup.watermark;

/** Event-time watermark generator. */
public interface WatermarkGenerator {
    long NO_WATERMARK = Long.MIN_VALUE;

    /** Observe a forwarded (first-occurrence) event time. */
    void observe(long eventTime);

    /** Current watermark. {@link #NO_WATERMARK} before any progress. */
    long watermark();

    /** Heuristic watermark derived only from observed event times
     *  (ignores explicit timer advances). Used by periodic emission. */
    long heuristicWatermark();

    /**
     * Explicitly advance the watermark to {@code target} (e.g. timer tick).
     * A target not greater than the current value is a regression and is
     * rejected; returns false in that case without changing state.
     */
    boolean tryAdvance(long target);

    void restore(long watermark, long maxObserved);
}
