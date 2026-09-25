package com.example.tjoin.model;

import java.util.List;

/**
 * Result of processing one input event: admission status, any join pairs
 * emitted at that moment, and (when relevant) the event that DROP_OLDEST
 * evicted.
 */
public record ProcessResult(
        AcceptStatus status,
        List<JoinResult> emitted,
        StreamEvent evicted,
        /** Number of stale records cleaned from both sides during this call. */
        int cleaned
) {
    public static ProcessResult of(AcceptStatus status, List<JoinResult> emitted, StreamEvent evicted, int cleaned) {
        return new ProcessResult(status, List.copyOf(emitted), evicted, cleaned);
    }

    public static ProcessResult rejected(AcceptStatus status) {
        return new ProcessResult(status, List.of(), null, 0);
    }
}
