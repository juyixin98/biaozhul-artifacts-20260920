package com.example.vic.domain;

/**
 * One non-overlapping effective segment of the computed coverage.
 * Retains the source version and rule it came from.
 */
public record Segment(long start, long end, String versionId, String ruleId, String label) {

    public Interval interval() {
        return new Interval(start, end);
    }
}
