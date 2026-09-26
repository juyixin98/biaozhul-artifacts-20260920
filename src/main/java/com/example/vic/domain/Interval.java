package com.example.vic.domain;

/**
 * Half-open interval [start, end) on a long axis.
 * For time rules, the axis is epoch seconds; for version rules, any ordered integer axis.
 */
public record Interval(long start, long end) {

    public Interval {
        if (end <= start) {
            throw new IllegalArgumentException(
                    "interval end must be greater than start: [" + start + ", " + end + ")");
        }
    }

    public boolean contains(long point) {
        return point >= start && point < end;
    }

    public boolean overlaps(Interval other) {
        return this.start < other.end && other.start < this.end;
    }
}
