package com.example.hlc.core;

/**
 * Explicit policy for what happens when the logical counter would exceed
 * {@code maxLogical} within a single physical millisecond.
 */
public enum OverflowPolicy {

    /**
     * Advance the physical component by one millisecond beyond the current
     * HLC physical value and reset the logical counter to zero. The clock
     * stays monotonic; the physical component temporarily runs ahead of the
     * wall clock until the wall clock catches up.
     */
    BUMP_PHYSICAL,

    /**
     * Throw {@link LogicalOverflowException}. The caller must retry after the
     * wall clock has advanced to the next millisecond.
     */
    THROW
}
