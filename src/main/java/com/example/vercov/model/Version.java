package com.example.vercov.model;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * A named, immutable set of interval rules published under one priority.
 * Higher priority wins where intervals overlap; two versions at the same
 * priority must never overlap (enforced by the store before insertion).
 *
 * @param id        unique version identifier
 * @param priority  stacking priority; larger values take precedence
 * @param intervals rules contributed by this version (defensively copied)
 */
public record Version(String id, int priority, List<IntervalRule> intervals) {

    public Version {
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("version id must not be blank");
        }
        if (intervals == null) {
            throw new IllegalArgumentException("intervals must not be null");
        }
        List<IntervalRule> sorted = new ArrayList<>(intervals);
        sorted.sort(Comparator.comparingLong(IntervalRule::start));
        for (int i = 1; i < sorted.size(); i++) {
            if (sorted.get(i - 1).overlaps(sorted.get(i))) {
                throw new IllegalArgumentException(
                        "version '" + id + "' has self-overlapping intervals: "
                                + sorted.get(i - 1) + " and " + sorted.get(i));
            }
        }
        intervals = List.copyOf(sorted);
    }
}
