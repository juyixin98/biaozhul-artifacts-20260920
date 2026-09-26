package com.example.vic.domain;

/**
 * A single rule contributed by a version: it claims the interval and carries an optional label.
 */
public record RuleDef(String id, String versionId, Interval interval, String label) {

    public RuleDef {
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("rule id must not be blank");
        }
        if (versionId == null || versionId.isBlank()) {
            throw new IllegalArgumentException("rule versionId must not be blank");
        }
        if (interval == null) {
            throw new IllegalArgumentException("rule interval must not be null");
        }
    }
}
