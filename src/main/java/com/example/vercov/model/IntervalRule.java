package com.example.vercov.model;

/**
 * A half-open interval rule {@code [start, end)} on a long axis
 * (integer positions, or epoch seconds when the axis represents time).
 *
 * @param start inclusive lower bound, must be strictly less than {@code end}
 * @param end   exclusive upper bound
 * @param label optional free-form payload carried by the rule (may be null)
 */
public record IntervalRule(long start, long end, String label) {

    public IntervalRule {
        if (start >= end) {
            throw new IllegalArgumentException(
                    "interval start must be < end, got [" + start + ", " + end + ")");
        }
    }

    public boolean overlaps(IntervalRule other) {
        return this.start < other.end && other.start < this.end;
    }

    public boolean contains(long point) {
        return start <= point && point < end;
    }
}
