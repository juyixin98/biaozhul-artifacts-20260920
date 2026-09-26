package com.migration.planner.plan;

import com.migration.planner.model.MigrationEdge;
import com.migration.planner.model.Precondition;
import com.migration.planner.model.SchemaGraph;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.TreeSet;

/**
 * Finds minimum-cost migration paths with Dijkstra over precondition-filtered edges.
 *
 * <p>Cost ties are broken deterministically by lexicographic node-id order, and every
 * equal-cost alternative (up to a cap) is enumerated over the tight-edge subgraph so
 * callers can see that a tie occurred. Cycles are safe: edge costs must be positive,
 * so tight edges strictly increase distance and enumeration cannot loop.
 */
public final class PathPlanner {

    public static final int DEFAULT_MAX_PATHS = 16;
    private static final int ENUMERATION_HARD_CAP = 1000;

    public record ScoredPath(List<String> nodes, List<MigrationEdge> edges, long totalCost) {
    }

    public record SearchResult(ScoredPath chosen, List<ScoredPath> alternatives, boolean truncated) {
    }

    private record NodeDist(String node, long dist) {
    }

    public SearchResult findBestPaths(SchemaGraph graph, String from, String to,
                                      Map<String, Boolean> context, int maxPaths) {
        if (!graph.nodes().contains(from)) {
            throw PlanException.unknownVersion(from);
        }
        if (!graph.nodes().contains(to)) {
            throw PlanException.unknownVersion(to);
        }
        if (from.equals(to)) {
            ScoredPath trivial = new ScoredPath(List.of(from), List.of(), 0L);
            return new SearchResult(trivial, List.of(), false);
        }

        Map<String, Long> distFrom = dijkstra(graph, from, context, false);
        Map<String, Long> distTo = dijkstra(graph, to, context, true);
        long best = distFrom.getOrDefault(to, Long.MAX_VALUE);
        if (best == Long.MAX_VALUE) {
            throw PlanException.noPath(from, to, reachableVersions(distFrom), blockedEdges(graph, context));
        }

        List<ScoredPath> paths = new ArrayList<>();
        enumerate(graph, from, to, context, distFrom, distTo, best, paths);
        paths.sort(PathPlanner::compareByNodes);

        boolean truncated = paths.size() > maxPaths;
        List<ScoredPath> kept = new ArrayList<>(paths.subList(0, Math.min(paths.size(), maxPaths)));
        ScoredPath chosen = kept.remove(0);
        return new SearchResult(chosen, List.copyOf(kept), truncated);
    }

    private Map<String, Long> dijkstra(SchemaGraph graph, String source,
                                       Map<String, Boolean> context, boolean reverse) {
        Map<String, Long> dist = new HashMap<>();
        PriorityQueue<NodeDist> queue = new PriorityQueue<>(
                Comparator.comparingLong(NodeDist::dist).thenComparing(NodeDist::node));
        dist.put(source, 0L);
        queue.add(new NodeDist(source, 0L));
        while (!queue.isEmpty()) {
            NodeDist current = queue.poll();
            if (current.dist() > dist.getOrDefault(current.node(), Long.MAX_VALUE)) {
                continue;
            }
            List<MigrationEdge> adjacent = reverse
                    ? graph.incomingTo(current.node())
                    : graph.outgoingFrom(current.node());
            for (MigrationEdge edge : adjacent) {
                if (!edge.usableWith(context)) {
                    continue;
                }
                String neighbor = reverse ? edge.from() : edge.to();
                long candidate = current.dist() + edge.cost();
                if (candidate < dist.getOrDefault(neighbor, Long.MAX_VALUE)) {
                    dist.put(neighbor, candidate);
                    queue.add(new NodeDist(neighbor, candidate));
                }
            }
        }
        return dist;
    }

    private void enumerate(SchemaGraph graph, String from, String to,
                           Map<String, Boolean> context,
                           Map<String, Long> distFrom, Map<String, Long> distTo,
                           long best, List<ScoredPath> out) {
        Deque<String> nodes = new ArrayDeque<>(List.of(from));
        Deque<MigrationEdge> edges = new ArrayDeque<>();
        enumerateRec(graph, to, context, distFrom, distTo, best, nodes, edges, out);
    }

    private void enumerateRec(SchemaGraph graph, String target,
                              Map<String, Boolean> context,
                              Map<String, Long> distFrom, Map<String, Long> distTo,
                              long best,
                              Deque<String> nodes, Deque<MigrationEdge> edges,
                              List<ScoredPath> out) {
        if (out.size() >= ENUMERATION_HARD_CAP) {
            return;
        }
        String here = nodes.peekLast();
        if (here.equals(target)) {
            out.add(new ScoredPath(List.copyOf(nodes), List.copyOf(edges), best));
            return;
        }
        for (MigrationEdge edge : graph.outgoingFrom(here)) {
            if (!edge.usableWith(context) || !isTight(edge, distFrom, distTo, best)) {
                continue;
            }
            nodes.addLast(edge.to());
            edges.addLast(edge);
            enumerateRec(graph, target, context, distFrom, distTo, best, nodes, edges, out);
            edges.removeLast();
            nodes.removeLast();
        }
    }

    private boolean isTight(MigrationEdge edge, Map<String, Long> distFrom,
                            Map<String, Long> distTo, long best) {
        long before = distFrom.getOrDefault(edge.from(), Long.MAX_VALUE);
        long after = distTo.getOrDefault(edge.to(), Long.MAX_VALUE);
        if (before == Long.MAX_VALUE || after == Long.MAX_VALUE) {
            return false;
        }
        return before + edge.cost() + after == best;
    }

    private static int compareByNodes(ScoredPath a, ScoredPath b) {
        int shared = Math.min(a.nodes().size(), b.nodes().size());
        for (int i = 0; i < shared; i++) {
            int c = a.nodes().get(i).compareTo(b.nodes().get(i));
            if (c != 0) {
                return c;
            }
        }
        return Integer.compare(a.nodes().size(), b.nodes().size());
    }

    private List<String> reachableVersions(Map<String, Long> distFrom) {
        TreeSet<String> reachable = new TreeSet<>();
        distFrom.forEach((node, d) -> {
            if (d < Long.MAX_VALUE) {
                reachable.add(node);
            }
        });
        return List.copyOf(reachable);
    }

    private List<Map<String, Object>> blockedEdges(SchemaGraph graph, Map<String, Boolean> context) {
        List<Map<String, Object>> blocked = new ArrayList<>();
        for (MigrationEdge edge : graph.edges()) {
            List<Precondition> unmet = edge.preconditions().stream()
                    .filter(p -> !p.satisfiedBy(context))
                    .toList();
            if (!unmet.isEmpty()) {
                Map<String, Object> entry = new HashMap<>();
                entry.put("edgeId", edge.id());
                entry.put("from", edge.from());
                entry.put("to", edge.to());
                entry.put("unmetPreconditions", unmet);
                blocked.add(entry);
            }
        }
        return blocked;
    }
}
