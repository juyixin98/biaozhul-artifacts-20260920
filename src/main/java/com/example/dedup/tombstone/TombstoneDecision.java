package com.example.dedup.tombstone;

/** Result of {@link TombstoneStore#lookup}. */
public enum TombstoneDecision {
    /** No covering live tombstone and none could have expired: upsert is live. */
    NONE,
    /** A live DELETE at or after the upsert time covers it: suppress. */
    SUPPRESSED,
    /** Tombstone state for the time range was already released: unknown. */
    UNCERTAIN
}
