package com.example.dedup.dedup;

/** Outcome of looking up an event id in bounded dedup state. */
public record DedupResult(
        boolean duplicate,
        boolean payloadMismatch,
        boolean eventTimeSkew,
        boolean unverified,
        boolean capacityEvicted,
        String evictedId) {

    static DedupResult firstSeen(boolean capacityEvicted, String evictedId) {
        return new DedupResult(false, false, false, false, capacityEvicted, evictedId);
    }
}
