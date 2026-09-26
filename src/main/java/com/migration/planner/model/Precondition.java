package com.migration.planner.model;

import java.util.Map;

/**
 * A named precondition on a migration edge: the request context must contain
 * {@code flag} with boolean value equal to {@code expected} for the edge to be usable.
 */
public record Precondition(String flag, boolean expected) {

    public boolean satisfiedBy(Map<String, Boolean> context) {
        return context.getOrDefault(flag, Boolean.FALSE) == expected;
    }
}
