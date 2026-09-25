package com.example.tjoin.model;

/**
 * Immutable configuration of a two-stream interval join.
 *
 * <p>A left event at time {@code tL} and a right event at time {@code tR}
 * match iff their keys are equal and
 * {@code lowerBound <= tR - tL <= upperBound}. Bounds are inclusive and may
 * be negative; {@code lowerBound <= upperBound} is required.</p>
 *
 * <h2>Per-side settings</h2>
 * Each side independently has:
 * <ul>
 *   <li>{@code maxOutOfOrderness} — bounded-out-of-orderness watermark lag:
 *       the side watermark is {@code maxSeenEventTime - maxOutOfOrderness}.</li>
 *   <li>{@code idleTimeoutMillis} — if no element arrives on the side for
 *       this long (in injectable processing time) the side is considered
 *       stalled/idle and stops advancing the join watermark. {@code <= 0}
 *       disables idleness detection.</li>
 *   <li>{@code maxBufferSize} — hard cap on buffered (not yet cleaned)
 *       events for the side; {@code <= 0} means unlimited.</li>
 * </ul>
 */
public record JoinConfig(
        long lowerBound,
        long upperBound,
        SideConfig left,
        SideConfig right
) {

    public record SideConfig(long maxOutOfOrderness,
                             long idleTimeoutMillis,
                             int maxBufferSize,
                             BufferOverflowPolicy overflowPolicy) {

        public SideConfig {
            if (maxOutOfOrderness < 0) {
                throw new IllegalArgumentException("maxOutOfOrderness must be >= 0");
            }
            if (overflowPolicy == null) {
                overflowPolicy = BufferOverflowPolicy.REJECT;
            }
        }

        public static SideConfig of(long maxOutOfOrderness,
                                    long idleTimeoutMillis,
                                    int maxBufferSize,
                                    BufferOverflowPolicy overflowPolicy) {
            return new SideConfig(maxOutOfOrderness, idleTimeoutMillis, maxBufferSize, overflowPolicy);
        }
    }

    public JoinConfig {
        if (lowerBound > upperBound) {
            throw new IllegalArgumentException(
                    "lowerBound (" + lowerBound + ") must be <= upperBound (" + upperBound + ")");
        }
        if (left == null || right == null) {
            throw new IllegalArgumentException("both side configs are required");
        }
    }

    public static JoinConfig of(long lowerBound, long upperBound, SideConfig left, SideConfig right) {
        return new JoinConfig(lowerBound, upperBound, left, right);
    }

    /** Convenience: identical settings on both sides. */
    public static JoinConfig symmetric(long lowerBound, long upperBound,
                                       long maxOutOfOrderness, long idleTimeoutMillis,
                                       int maxBufferSize, BufferOverflowPolicy overflowPolicy) {
        SideConfig side = SideConfig.of(maxOutOfOrderness, idleTimeoutMillis, maxBufferSize, overflowPolicy);
        return new JoinConfig(lowerBound, upperBound, side, side);
    }
}
