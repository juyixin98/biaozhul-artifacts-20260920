package com.example.migration.model;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;
import java.util.Map;

/**
 * A single declared migration edge in the schema version directed graph.
 *
 * <p>{@code reversible} is vendor-supplied metadata only. It is NEVER treated as
 * proof that a reverse migration can be executed: rollback planning requires a
 * real edge directed from the higher version back (see PathPlanner).
 *
 * @param cost          positive cost weight (downtime minutes, risk points, ...)
 * @param reversible    informational flag; does not synthesize a reverse edge
 * @param preconditions required request attributes; value = scalar or allowed list
 * @param validFrom     optional earliest instant at which the edge may be traversed
 * @param validTo       optional latest instant at which the edge may be traversed
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record Edge(
        String from,
        String to,
        double cost,
        boolean reversible,
        Map<String, Object> preconditions,
        Instant validFrom,
        Instant validTo,
        String description
) {
    public Edge {
        if (from == null || from.isBlank()) {
            throw new IllegalArgumentException("edge.from is required");
        }
        if (to == null || to.isBlank()) {
            throw new IllegalArgumentException("edge.to is required");
        }
        if (from.equals(to)) {
            throw new IllegalArgumentException("self-loop edge is not allowed: " + from);
        }
        if (!(cost > 0) || Double.isNaN(cost) || Double.isInfinite(cost)) {
            throw new IllegalArgumentException(
                    "edge cost must be a finite positive number: " + from + "->" + to);
        }
        if (validFrom != null && validTo != null && validFrom.isAfter(validTo)) {
            throw new IllegalArgumentException(
                    "edge validFrom is after validTo: " + from + "->" + to);
        }
        preconditions = preconditions == null ? Map.of() : Map.copyOf(preconditions);
    }

    /** Convenience for tests / data builders. */
    public static Edge of(String from, String to, double cost, boolean reversible) {
        return new Edge(from, to, cost, reversible, null, null, null, null);
    }
}
