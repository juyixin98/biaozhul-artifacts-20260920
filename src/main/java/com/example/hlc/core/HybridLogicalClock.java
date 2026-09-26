package com.example.hlc.core;

import com.example.hlc.clock.PhysicalClock;

import java.util.Objects;
import java.util.Optional;

/**
 * Hybrid Logical Clock (HLC) after Kulkarni et al. Maintains a timestamp of
 * {@code (physicalMillis, logical)} that is:
 *
 * <ul>
 *   <li>close to physical wall-clock time, and</li>
 *   <li>strictly monotonic per node, even when the physical clock jumps
 *       backwards, and</li>
 *   <li>causality-preserving: if event a happens-before event b, then
 *       {@code ts(a) < ts(b)}.</li>
 * </ul>
 *
 * <p>All mutating methods are synchronized; instances are thread-safe.</p>
 */
public final class HybridLogicalClock {

    /** Default logical counter bound: 12 bits (0..4095). */
    public static final int DEFAULT_MAX_LOGICAL = 4095;

    private final PhysicalClock clock;
    private final String nodeId;
    private final int maxLogical;
    private final OverflowPolicy overflowPolicy;
    private final long maxDriftMillis;

    private long lastPhysical;
    private int logical;

    private HybridLogicalClock(Builder builder) {
        this.clock = Objects.requireNonNull(builder.clock, "clock");
        this.nodeId = Objects.requireNonNull(builder.nodeId, "nodeId");
        if (builder.maxLogical < 1) {
            throw new IllegalArgumentException("maxLogical must be >= 1, got " + builder.maxLogical);
        }
        this.maxLogical = builder.maxLogical;
        this.overflowPolicy = Objects.requireNonNull(builder.overflowPolicy, "overflowPolicy");
        this.maxDriftMillis = builder.maxDriftMillis;
        this.lastPhysical = 0L;
        this.logical = 0;
        builder.restored.ifPresent(state -> {
            this.lastPhysical = state.physicalMillis();
            this.logical = state.logical();
        });
    }

    public static Builder builder(PhysicalClock clock, String nodeId) {
        return new Builder(clock, nodeId);
    }

    /**
     * Local or send event: returns a fresh timestamp guaranteed to be greater
     * than every timestamp previously produced or observed by this clock.
     */
    public synchronized HlcTimestamp tick() {
        long wallNow = clock.millis();
        if (wallNow > lastPhysical) {
            // Physical clock moved forward (normal case): adopt it.
            lastPhysical = wallNow;
            logical = 0;
        } else {
            // Physical clock stalled or moved backwards: stay monotonic by
            // bumping the logical counter instead of following the wall clock.
            incrementLogical();
        }
        return current();
    }

    /**
     * Receive event: merge a remote timestamp into this clock and return the
     * resulting fresh timestamp.
     *
     * @throws ClockDriftException if the remote physical time is further ahead
     *                             of the local wall clock than maxDriftMillis
     */
    public synchronized HlcTimestamp receive(HlcTimestamp remote) {
        Objects.requireNonNull(remote, "remote timestamp must not be null");
        long wallNow = clock.millis();
        if (maxDriftMillis > 0 && remote.physicalMillis() - wallNow > maxDriftMillis) {
            throw new ClockDriftException(
                    "remote physical time " + remote.physicalMillis()
                            + " is more than " + maxDriftMillis + "ms ahead of local wall clock " + wallNow);
        }
        if (wallNow > lastPhysical && wallNow > remote.physicalMillis()) {
            lastPhysical = wallNow;
            logical = 0;
        } else if (lastPhysical == remote.physicalMillis()) {
            logical = Math.max(logical, remote.logical());
            incrementLogical();
        } else if (remote.physicalMillis() > lastPhysical) {
            lastPhysical = remote.physicalMillis();
            logical = remote.logical();
            incrementLogical();
        } else {
            incrementLogical();
        }
        return current();
    }

    /** Current timestamp without advancing the clock. */
    public synchronized HlcTimestamp current() {
        return new HlcTimestamp(lastPhysical, logical, nodeId);
    }

    public String nodeId() {
        return nodeId;
    }

    public int maxLogical() {
        return maxLogical;
    }

    public OverflowPolicy overflowPolicy() {
        return overflowPolicy;
    }

    public long maxDriftMillis() {
        return maxDriftMillis;
    }

    /** Increments the logical counter, applying the configured overflow policy. */
    private void incrementLogical() {
        if (logical < maxLogical) {
            logical++;
            return;
        }
        switch (overflowPolicy) {
            case BUMP_PHYSICAL -> {
                // Explicit overflow handling: move the physical component one
                // millisecond into the future and restart the logical counter.
                lastPhysical++;
                logical = 0;
            }
            case THROW -> throw new LogicalOverflowException(
                    "logical counter exceeded maxLogical=" + maxLogical
                            + " at physical=" + lastPhysical + " on node " + nodeId);
        }
    }

    public static final class Builder {
        private final PhysicalClock clock;
        private final String nodeId;
        private int maxLogical = DEFAULT_MAX_LOGICAL;
        private OverflowPolicy overflowPolicy = OverflowPolicy.BUMP_PHYSICAL;
        private long maxDriftMillis = 0L; // 0 = drift check disabled
        private Optional<HlcTimestamp> restored = Optional.empty();

        private Builder(PhysicalClock clock, String nodeId) {
            this.clock = clock;
            this.nodeId = nodeId;
        }

        public Builder maxLogical(int maxLogical) {
            this.maxLogical = maxLogical;
            return this;
        }

        public Builder overflowPolicy(OverflowPolicy policy) {
            this.overflowPolicy = policy;
            return this;
        }

        public Builder maxDriftMillis(long maxDriftMillis) {
            this.maxDriftMillis = maxDriftMillis;
            return this;
        }

        /** Restore previously persisted state (monotonicity is preserved even if the wall clock is behind it). */
        public Builder restored(Optional<HlcTimestamp> restored) {
            this.restored = restored == null ? Optional.empty() : restored;
            return this;
        }

        public HybridLogicalClock build() {
            return new HybridLogicalClock(this);
        }
    }
}
