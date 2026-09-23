package approx.stream;

/** Static configuration for a {@link FrequentItemsEngine}. */
public final class EngineConfig {

    /** Sketch width ({@code width = ceil(e/epsilon)} when sized from an error target). */
    public final int width;
    /** Sketch depth ({@code depth = ceil(ln(1/delta))} when sized from a confidence target). */
    public final int depth;
    /** Hash-family seed; sketches with different seeds can never be merged. */
    public final long seed;
    /** Maximum number of retained candidates per window. */
    public final int candidateCapacity;
    /**
     * Tumbling-window length in milliseconds; {@code 0} means the engine keeps a
     * single never-rotating window.
     */
    public final long windowMillis;
    /** Whether to also keep an exact per-item counter as a small-data reference. */
    public final boolean trackExact;

    private EngineConfig(int width, int depth, long seed, int candidateCapacity,
                         long windowMillis, boolean trackExact) {
        if (width <= 0 || depth <= 0) {
            throw new IllegalArgumentException("width and depth must be positive");
        }
        if (candidateCapacity <= 0) {
            throw new IllegalArgumentException("candidateCapacity must be positive");
        }
        if (windowMillis < 0) {
            throw new IllegalArgumentException("windowMillis must be >= 0 (0 = no rotation)");
        }
        this.width = width;
        this.depth = depth;
        this.seed = seed;
        this.candidateCapacity = candidateCapacity;
        this.windowMillis = windowMillis;
        this.trackExact = trackExact;
    }

    public static Builder builder() {
        return new Builder();
    }

    /** Build from an error target: epsilon (additive error / N) and delta (failure probability). */
    public static Builder withErrorTarget(double epsilon, double delta) {
        if (!(epsilon > 0 && epsilon < 1) || !(delta > 0 && delta < 1)) {
            throw new IllegalArgumentException("require 0 < epsilon < 1 and 0 < delta < 1");
        }
        int width = (int) Math.ceil(Math.E / epsilon);
        int depth = (int) Math.ceil(Math.log(1.0 / delta));
        return builder().sketchDimensions(width, depth);
    }

    public static final class Builder {
        private int width = 64;
        private int depth = 5;
        private long seed = 0L;
        private int candidateCapacity = 10;
        private long windowMillis = 0L;
        private boolean trackExact = false;

        public Builder sketchDimensions(int width, int depth) {
            this.width = width;
            this.depth = depth;
            return this;
        }

        public Builder seed(long seed) {
            this.seed = seed;
            return this;
        }

        public Builder candidateCapacity(int capacity) {
            this.candidateCapacity = capacity;
            return this;
        }

        public Builder windowMillis(long windowMillis) {
            this.windowMillis = windowMillis;
            return this;
        }

        public Builder trackExact(boolean trackExact) {
            this.trackExact = trackExact;
            return this;
        }

        public EngineConfig build() {
            return new EngineConfig(width, depth, seed, candidateCapacity, windowMillis, trackExact);
        }
    }
}
