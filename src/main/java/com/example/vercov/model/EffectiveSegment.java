package com.example.vercov.model;

/**
 * One non-overlapping piece of the final effective coverage, keeping the
 * version it originates from.
 *
 * @param start     inclusive lower bound
 * @param end       exclusive upper bound
 * @param versionId source version that produced this segment
 * @param priority  priority of the source version
 * @param label     label carried by the source interval rule (may be null)
 */
public record EffectiveSegment(long start, long end, String versionId, int priority, String label) {
}
