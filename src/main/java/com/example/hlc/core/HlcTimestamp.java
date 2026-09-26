package com.example.hlc.core;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Objects;

/**
 * A Hybrid Logical Clock timestamp: physical wall-clock milliseconds plus a
 * logical counter that orders events sharing the same physical millisecond.
 *
 * <p>Ordering is lexicographic on {@code (physicalMillis, logical, nodeId)}.
 * The {@code nodeId} tie-breaker only makes the order total; two timestamps
 * that differ are <em>not</em> necessarily causally related — HLC ordering
 * implies causality in one direction only (causally-ordered events get
 * ordered timestamps, never the converse).</p>
 */
public record HlcTimestamp(
        @JsonProperty("physicalMillis") long physicalMillis,
        @JsonProperty("logical") int logical,
        @JsonProperty("nodeId") String nodeId)
        implements Comparable<HlcTimestamp> {

    @JsonCreator
    public HlcTimestamp {
        Objects.requireNonNull(nodeId, "nodeId must not be null");
        if (physicalMillis < 0) {
            throw new IllegalArgumentException("physicalMillis must be >= 0, got " + physicalMillis);
        }
        if (logical < 0) {
            throw new IllegalArgumentException("logical must be >= 0, got " + logical);
        }
    }

    @Override
    public int compareTo(HlcTimestamp other) {
        int byPhysical = Long.compare(this.physicalMillis, other.physicalMillis);
        if (byPhysical != 0) {
            return byPhysical;
        }
        int byLogical = Integer.compare(this.logical, other.logical);
        if (byLogical != 0) {
            return byLogical;
        }
        return this.nodeId.compareTo(other.nodeId);
    }

    @Override
    public String toString() {
        return "HLC{physical=" + physicalMillis + ", logical=" + logical + ", node=" + nodeId + "}";
    }
}
