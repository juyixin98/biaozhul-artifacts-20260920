package com.migration.planner.model;

import java.util.List;

/** One migration step in a planned path. */
public record PlanStep(
        int seq,
        String edgeId,
        String from,
        String to,
        long cost,
        String description,
        List<Precondition> preconditions) {
}
