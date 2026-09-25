package invidx.engine;

import java.time.Duration;

/** Tunable engine parameters. */
public record IndexConfig(
        int maxBufferedDocs,
        boolean backgroundMerge,
        Duration mergeInterval,
        int mergeFactor) {

    public IndexConfig {
        if (maxBufferedDocs <= 0) {
            throw new IllegalArgumentException("maxBufferedDocs must be positive");
        }
        if (mergeFactor < 2) {
            throw new IllegalArgumentException("mergeFactor must be >= 2");
        }
        mergeInterval = mergeInterval == null ? Duration.ofSeconds(1) : mergeInterval;
    }

    public static IndexConfig defaults() {
        return new IndexConfig(8, true, Duration.ofSeconds(1), 3);
    }

    public static IndexConfig testing() {
        // Fast ticks, tiny buffer so merge/flush interleavings happen quickly.
        return new IndexConfig(4, true, Duration.ofMillis(100), 3);
    }
}
