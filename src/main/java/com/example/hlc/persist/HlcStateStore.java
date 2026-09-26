package com.example.hlc.persist;

import com.example.hlc.core.HlcTimestamp;

import java.util.Optional;

/** Durable store for the last known HLC state, used to survive restarts. */
public interface HlcStateStore {

    /** Persist the given timestamp as the latest known state. */
    void save(HlcTimestamp timestamp);

    /** Load the previously persisted state, or empty if none exists / unreadable. */
    Optional<HlcTimestamp> load();
}
