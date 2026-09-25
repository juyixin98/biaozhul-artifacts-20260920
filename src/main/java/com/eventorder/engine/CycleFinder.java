package com.eventorder.engine;

import com.eventorder.model.Edge;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Finds one cycle in the ordering graph, if any. Iterative DFS over nodes and
 * neighbours in sorted order, so the reported cycle is deterministic.
 * The returned edge chain is the minimal readable witness: each edge carries
 * the rule and reason that created it.
 */
public final class CycleFinder {

    private enum Color { WHITE, GRAY, BLACK }

    private CycleFinder() {
    }

    public static Optional<List<Edge>> findCycle(GraphBuilder.BuiltGraph graph) {
        Map<String, Color> color = new HashMap<>();
        for (String id : graph.nodeIds()) {
            color.put(id, Color.WHITE);
        }
        for (String start : graph.nodeIds()) {
            if (color.get(start) == Color.WHITE) {
                Optional<List<Edge>> cycle = dfs(start, graph, color);
                if (cycle.isPresent()) {
                    return cycle;
                }
            }
        }
        return Optional.empty();
    }

    private static Optional<List<Edge>> dfs(String start, GraphBuilder.BuiltGraph graph,
                                            Map<String, Color> color) {
        // Stack frames: the edge used to reach the node, and the node itself.
        Deque<Edge> edgeStack = new ArrayDeque<>();
        Deque<String> nodeStack = new ArrayDeque<>();
        Deque<Integer> childIndex = new ArrayDeque<>();

        color.put(start, Color.GRAY);
        nodeStack.push(start);
        childIndex.push(0);

        while (!nodeStack.isEmpty()) {
            String node = nodeStack.peek();
            List<Edge> neighbours = graph.adjacency().getOrDefault(node, List.of());
            int idx = childIndex.pop();
            if (idx >= neighbours.size()) {
                color.put(node, Color.BLACK);
                nodeStack.pop();
                // Invariant: edgeStack.size() == nodeStack.size() - 1 while descending,
                // so after popping a finished node exactly one edge must leave the stack.
                if (!edgeStack.isEmpty() && edgeStack.size() == nodeStack.size()) {
                    edgeStack.pop();
                }
                continue;
            }
            childIndex.push(idx + 1);
            Edge edge = neighbours.get(idx);
            String next = edge.to();
            switch (color.get(next)) {
                case WHITE -> {
                    color.put(next, Color.GRAY);
                    edgeStack.push(edge);
                    nodeStack.push(next);
                    childIndex.push(0);
                }
                case GRAY -> {
                    return Optional.of(extractCycle(edgeStack, edge, next));
                }
                case BLACK -> {
                    // cross/forward edge: no cycle here
                }
            }
        }
        return Optional.empty();
    }

    /** Builds the cycle edge chain ending at the back edge that closes the loop. */
    private static List<Edge> extractCycle(Deque<Edge> edgeStack, Edge closingEdge, String cycleStart) {
        List<Edge> path = new ArrayList<>();
        edgeStack.descendingIterator().forEachRemaining(path::add);
        int startIdx = -1;
        for (int i = 0; i < path.size(); i++) {
            if (path.get(i).from().equals(cycleStart)) {
                startIdx = i;
                break;
            }
        }
        List<Edge> cycle = new ArrayList<>(path.subList(startIdx < 0 ? 0 : startIdx, path.size()));
        cycle.add(closingEdge);
        return cycle;
    }
}
