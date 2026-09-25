package com.eventorder.engine;

import com.eventorder.model.Edge;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Kahn's algorithm with a lexicographic tie-break: whenever several events are
 * simultaneously orderable, the smallest id is emitted first. This makes the
 * output a deterministic function of the input, independent of map iteration
 * order or JVM identity hashes.
 */
public final class TopoSorter {

    /** @param order             deterministic topological order
     * @param multipleValidOrders true iff the constraints admit more than one
     *                            linear extension (i.e. at some step more than
     *                            one event was simultaneously orderable) */
    public record SortOutcome(List<String> order, boolean multipleValidOrders) {
    }

    private TopoSorter() {
    }

    public static SortOutcome sort(GraphBuilder.BuiltGraph graph) {
        Map<String, Integer> indegree = new HashMap<>();
        for (String id : graph.nodeIds()) {
            indegree.put(id, 0);
        }
        for (Map.Entry<String, List<Edge>> entry : graph.adjacency().entrySet()) {
            for (Edge edge : entry.getValue()) {
                indegree.merge(edge.to(), 1, Integer::sum);
            }
        }

        PriorityQueue<String> ready = new PriorityQueue<>(graph.nodeIds().stream()
                .filter(id -> indegree.get(id) == 0)
                .toList());

        List<String> order = new ArrayList<>(graph.nodeIds().size());
        boolean multiple = false;
        while (!ready.isEmpty()) {
            if (ready.size() > 1) {
                multiple = true;
            }
            String node = ready.poll();
            order.add(node);
            for (Edge edge : graph.adjacency().getOrDefault(node, List.of())) {
                int remaining = indegree.merge(edge.to(), -1, Integer::sum);
                if (remaining == 0) {
                    ready.add(edge.to());
                }
            }
        }
        if (order.size() != graph.nodeIds().size()) {
            throw new IllegalStateException("graph contains a cycle; run CycleFinder first");
        }
        return new SortOutcome(List.copyOf(order), multiple);
    }
}
