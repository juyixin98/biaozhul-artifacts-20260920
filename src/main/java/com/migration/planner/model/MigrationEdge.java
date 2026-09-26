package com.migration.planner.model;

import java.util.List;
import java.util.Map;

/**
 * One directed migration edge between two schema versions.
 *
 * <p>{@code reversible} is author-declared metadata only. The planner never treats it
 * as proof that a rollback exists: a rollback step is only reported when a real
 * inverse edge (to -&gt; from) is present in the graph.
 */
public record MigrationEdge(
        String id,
        String from,
        String to,
        long cost,
        boolean reversible,
        String description,
        List<Precondition> preconditions) {

    public MigrationEdge {
        preconditions = preconditions == null ? List.of() : List.copyOf(preconditions);
        description = description == null ? "" : description;
    }

    public boolean usableWith(Map<String, Boolean> context) {
        for (Precondition p : preconditions) {
            if (!p.satisfiedBy(context)) {
                return false;
            }
        }
        return true;
    }
}
