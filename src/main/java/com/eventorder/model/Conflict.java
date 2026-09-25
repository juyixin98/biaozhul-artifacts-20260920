package com.eventorder.model;

import java.util.List;

/** One unsatisfiable conflict: a minimal cycle expressed as an ordered chain of edges. */
public record Conflict(String type, List<Edge> chain, String message) {

    public static Conflict cycle(List<Edge> cycleEdges) {
        StringBuilder path = new StringBuilder();
        for (int i = 0; i < cycleEdges.size(); i++) {
            if (i > 0) {
                path.append(" -> ");
            }
            path.append(cycleEdges.get(i).from());
        }
        path.append(" -> ").append(cycleEdges.get(cycleEdges.size() - 1).to());
        return new Conflict("CYCLE", List.copyOf(cycleEdges),
                "circular ordering constraints: " + path);
    }
}
