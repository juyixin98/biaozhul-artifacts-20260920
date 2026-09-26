package com.example.migration.model;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * Planning request.
 *
 * @param graph              schema version graph (nodes are opaque ids)
 * @param from               start version id
 * @param to                 target version id
 * @param at                 evaluation instant for edge time windows (UTC)
 * @param attributes         request attributes matched against edge preconditions
 * @param mode               UPGRADE or ROLLBACK (informational; only real edges are ever traversed)
 * @param requireReversible  when true, edges flagged reversible=false are excluded (UPGRADE guard)
 * @param maxPaths           maximum number of alternative simple paths to return (1..50)
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record PlanRequest(
        Graph graph,
        String from,
        String to,
        Instant at,
        Map<String, Object> attributes,
        Mode mode,
        Boolean requireReversible,
        Integer maxPaths
) {
    public enum Mode { UPGRADE, ROLLBACK }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Graph(List<String> nodes, List<Edge> edges) {
    }

    public PlanRequest {
        if (graph == null) {
            throw new IllegalArgumentException("field 'graph' is required");
        }
        if (graph.edges() == null || graph.edges().isEmpty()) {
            throw new IllegalArgumentException("graph.edges must contain at least one edge");
        }
        if (from == null || from.isBlank()) {
            throw new IllegalArgumentException("field 'from' is required");
        }
        if (to == null || to.isBlank()) {
            throw new IllegalArgumentException("field 'to' is required");
        }
        attributes = attributes == null ? Map.of() : Map.copyOf(attributes);
        mode = mode == null ? Mode.UPGRADE : mode;
        maxPaths = maxPaths == null ? 3 : maxPaths;
        if (maxPaths < 1 || maxPaths > 50) {
            throw new IllegalArgumentException("maxPaths must be between 1 and 50");
        }
    }
}
