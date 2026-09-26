package com.example.eventorder.engine;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.TreeMap;

/**
 * Deterministic topological ordering: Kahn's algorithm with a min-heap on event
 * ids, producing the lexicographically smallest linear extension. Ties never
 * depend on hash or insertion order.
 */
public final class TopologicalOrderer {

    private TopologicalOrderer() {
    }

    /**
     * @return the lexicographically smallest topological order, or null when the
     *         graph contains a cycle
     */
    public static List<String> lexicographicOrder(DependencyGraph graph) {
        Map<String, Integer> indegree = new TreeMap<>(graph.indegrees());
        PriorityQueue<String> ready = new PriorityQueue<>();
        indegree.forEach((id, degree) -> {
            if (degree == 0) {
                ready.add(id);
            }
        });
        List<String> order = new ArrayList<>();
        while (!ready.isEmpty()) {
            String id = ready.poll();
            order.add(id);
            for (String next : graph.successorsOf(id)) {
                int remaining = indegree.merge(next, -1, Integer::sum);
                if (remaining == 0) {
                    ready.add(next);
                }
            }
        }
        return order.size() == indegree.size() ? order : null;
    }
}
