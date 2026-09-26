package com.example.intervals.model;

import java.util.Collections;
import java.util.List;

/**
 * An immutable set of points on a domain, represented as a list of
 * {@link Interval}s. Instances produced by {@code IntervalAlgebra} are always
 * <strong>normalized</strong>: intervals are disjoint, non-adjacent (when a
 * point gap exists), sorted by their lower cut, and free of empties. A
 * normalized representation is unique for a given point set, which makes
 * equality and JSON serialization stable.
 */
public final class IntervalSet<T extends Comparable<? super T>> {

    private final List<Interval<T>> intervals;

    public IntervalSet(List<Interval<T>> intervals) {
        this.intervals = Collections.unmodifiableList(List.copyOf(intervals));
    }

    public static <T extends Comparable<? super T>> IntervalSet<T> of(List<Interval<T>> intervals) {
        return new IntervalSet<>(intervals);
    }

    public List<Interval<T>> intervals() {
        return intervals;
    }

    public boolean isEmpty() {
        return intervals.isEmpty();
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof IntervalSet<?> that)) {
            return false;
        }
        return intervals.equals(that.intervals);
    }

    @Override
    public int hashCode() {
        return intervals.hashCode();
    }

    @Override
    public String toString() {
        return intervals.toString();
    }
}
