package com.example.migration.service;

import com.example.migration.model.Edge;
import com.example.migration.model.MigrationGraph;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse.BlockedEdge;
import com.example.migration.model.PlanResponse.Checkpoint;
import com.example.migration.model.PlanResponse.PathResult;
import com.example.migration.model.PlanResponse.StepResult;
import com.example.migration.model.PlanResponse.TimeWindow;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * Plans migration paths over a directed version graph.
 *
 * <p>Core guarantees:
 * <ul>
 *   <li>Only <b>real declared directed edges</b> are traversed. Version ids are
 *       opaque; nothing is inferred from their lexical/numeric magnitude.</li>
 *   <li>An edge flagged {@code reversible=true} never synthesizes a reverse edge.
 *       ROLLBACK from B to A is possible iff an edge B&rarr;A (or a real path of
 *       such edges) exists and satisfies constraints.</li>
 *   <li>Cycles are handled: only simple paths (no repeated version) are
 *       enumerated, so enumeration always terminates.</li>
 *   <li>Result ordering is deterministic: total cost, then step count, then
 *       lexical path signature — equal-cost alternatives are both returned
 *       (subject to {@code maxPaths}) rather than broken arbitrarily.</li>
 * </ul>
 */
public final class PathPlanner {

    /** Safety valve for combinatorial graphs; test data stays far below this. */
    private static final int ENUMERATION_EXPANSIONS_CAP = 200_000;

    // ---------------------------------------------------------------- evaluation

    /** All independent reasons why an edge cannot be used for this request. */
    public List<String> evaluate(Edge edge, PlanRequest request, Instant at, MigrationGraph graph) {
        List<String> reasons = new ArrayList<>();

        for (Map.Entry<String, Object> rule : edge.preconditions().entrySet()) {
            String key = rule.getKey();
            Object expected = rule.getValue();
            Object actual = request.attributes().get(key);
            if (actual == null) {
                reasons.add("precondition '" + key + "' not satisfied: attribute missing");
            } else if (!matches(actual, expected)) {
                reasons.add("precondition '" + key + "' not satisfied: value=" + actual
                        + ", allowed=" + expected);
            }
        }

        if (edge.validFrom() != null && at.isBefore(edge.validFrom())) {
            reasons.add("time window: edge not open until " + edge.validFrom() + " (at " + at + ")");
        }
        if (edge.validTo() != null && at.isAfter(edge.validTo())) {
            reasons.add("time window: edge closed after " + edge.validTo() + " (at " + at + ")");
        }

        if (Boolean.TRUE.equals(request.requireReversible()) && !edge.reversible()) {
            // UPGRADE guard. In ROLLBACK mode this is evaluated against the
            // forward counterpart below; a raw reverse edge is never blocked
            // merely for carrying reversible=false itself.
            if (request.mode() == PlanRequest.Mode.UPGRADE) {
                reasons.add("guard requireReversible: edge is marked reversible=false");
            }
        }

        if (request.mode() == PlanRequest.Mode.ROLLBACK
                && Boolean.TRUE.equals(request.requireReversible())) {
            // Strict opt-in audit on top of the hard rule (real edges only):
            // the traversed reverse edge B->A must have a real forward
            // counterpart A->B that is explicitly marked reversible=true.
            boolean counterpartExists = graph.hasDirectedEdge(edge.to(), edge.from());
            if (!counterpartExists) {
                reasons.add("rollback guard requireReversible: no real forward counterpart edge "
                        + edge.to() + "->" + edge.from());
            } else {
                boolean marked = graph.edges().stream()
                        .anyMatch(e -> e.from().equals(edge.to()) && e.to().equals(edge.from())
                                && e.reversible());
                if (!marked) {
                    reasons.add("rollback guard requireReversible: counterpart edge "
                            + edge.to() + "->" + edge.from()
                            + " is not marked reversible=true");
                }
            }
        }
        return reasons;
    }

    private static boolean matches(Object actual, Object expected) {
        if (expected instanceof List<?> allowed) {
            for (Object option : allowed) {
                if (valueEquals(actual, option)) {
                    return true;
                }
            }
            return false;
        }
        return valueEquals(actual, expected);
    }

    private static boolean valueEquals(Object actual, Object expected) {
        if (Objects.equals(actual, expected)) {
            return true;
        }
        // tolerate "1" vs 1 style encoding differences in JSON test data
        return String.valueOf(actual).equals(String.valueOf(expected));
    }

    // ----------------------------------------------------------------- planning

    public Plan plan(MigrationGraph graph, PlanRequest request, Instant at) {
        List<BlockedEdge> blocked = new ArrayList<>();
        Map<Edge, List<String>> edgeFailures = new LinkedHashMap<>();
        for (Edge edge : graph.edges()) {
            List<String> reasons = evaluate(edge, request, at, graph);
            if (!reasons.isEmpty()) {
                edgeFailures.put(edge, reasons);
                blocked.add(new BlockedEdge(edge.from(), edge.to(), reasons));
            }
        }

        Set<Edge> usable = new HashSet<>(graph.edges());
        usable.removeAll(edgeFailures.keySet());

        List<List<Edge>> paths = new ArrayList<>();
        boolean truncated = enumerateSimplePaths(graph, request.from(), request.to(),
                usable, paths);

        List<PathResult> results = rank(graph, paths);
        int limit = Math.min(request.maxPaths(), results.size());
        List<PathResult> top = results.subList(0, limit);

        List<String> warnings = new ArrayList<>();
        if (truncated) {
            warnings.add("path enumeration hit safety cap (" + ENUMERATION_EXPANSIONS_CAP
                    + " expansions); returned paths may not be exhaustive");
        }
        List<CycleInfo> cycles = detectCycles(graph, usable);
        if (!cycles.isEmpty()) {
            warnings.add("graph contains " + cycles.size()
                    + " reachable cycle(s); only simple (loop-free) paths are enumerated");
        }

        return new Plan(top, blocked, warnings, cycles);
    }

    public record Plan(List<PathResult> paths, List<BlockedEdge> blockedEdges,
                       List<String> warnings, List<CycleInfo> cycles) {
    }

    public record CycleInfo(List<String> vertices) {
    }

    /** Enumerate all simple paths {@code source -> target} using only usable edges. */
    private boolean enumerateSimplePaths(MigrationGraph graph, String source, String target,
                                         Set<Edge> usable, List<List<Edge>> sink) {
        int[] expansions = {0};
        Set<String> visited = new HashSet<>();
        List<Edge> trail = new ArrayList<>();
        return dfs(graph, source, target, usable, visited, trail, sink, expansions);
    }

    private boolean dfs(MigrationGraph graph, String current, String target, Set<Edge> usable,
                        Set<String> visited, List<Edge> trail, List<List<Edge>> sink,
                        int[] expansions) {
        if (current.equals(target)) {
            sink.add(List.copyOf(trail));
            return false;
        }
        if (expansions[0]++ >= ENUMERATION_EXPANSIONS_CAP) {
            return true;
        }
        visited.add(current);
        boolean truncated = false;
        for (Edge edge : graph.outgoingFrom(current)) {
            if (!usable.contains(edge) || visited.contains(edge.to())) {
                continue;
            }
            trail.add(edge);
            truncated = dfs(graph, edge.to(), target, usable, visited, trail, sink, expansions);
            trail.remove(trail.size() - 1);
            if (truncated) {
                break;
            }
        }
        visited.remove(current);
        return truncated;
    }

    private List<PathResult> rank(MigrationGraph graph, List<List<Edge>> paths) {
        List<PathResult> results = new ArrayList<>();
        int rank = 1;
        for (List<Edge> path : paths) {
            results.add(toPathResult(rank++, path, graph));
        }
        results.sort(Comparator
                .comparingDouble(PathResult::totalCost)
                .thenComparingInt(PathResult::stepCount)
                .thenComparing(p -> signature(p)));
        List<PathResult> reranked = new ArrayList<>();
        for (int i = 0; i < results.size(); i++) {
            PathResult p = results.get(i);
            reranked.add(new PathResult(i + 1, p.totalCost(), p.stepCount(),
                    p.initialCheckpoint(), p.steps(), p.finalCheckpoint()));
        }
        return reranked;
    }

    private static String signature(PathResult p) {
        StringBuilder sb = new StringBuilder(p.initialCheckpoint().version());
        for (StepResult s : p.steps()) {
            sb.append('>').append(s.to());
        }
        return sb.toString();
    }

    private PathResult toPathResult(int rank, List<Edge> path, MigrationGraph graph) {
        double total = 0;
        List<StepResult> steps = new ArrayList<>();
        String start = path.get(0).from();
        Checkpoint initial = new Checkpoint("CP-0", start, 0,
                "starting version verified; no edge inferred from version numbers");
        Checkpoint current = initial;
        for (int i = 0; i < path.size(); i++) {
            Edge edge = path.get(i);
            total = round(total + edge.cost());
            boolean reverseEdgeExists = graph.hasDirectedEdge(edge.to(), edge.from());
            Boolean reverseMarked = graph.edges().stream()
                    .filter(e -> e.from().equals(edge.to()) && e.to().equals(edge.from()))
                    .map(Edge::reversible)
                    .findFirst().orElse(null);
            current = new Checkpoint("CP-" + (i + 1), edge.to(), total,
                    "after step " + (i + 1) + ": " + edge.from() + "->" + edge.to());
            steps.add(new StepResult(
                    i + 1,
                    "MIGRATE",
                    edge.from(),
                    edge.to(),
                    edge.cost(),
                    edge.description(),
                    (edge.validFrom() != null || edge.validTo() != null)
                            ? new TimeWindow(edge.validFrom(), edge.validTo()) : null,
                    reverseEdgeExists,
                    reverseMarked,
                    current));
        }
        return new PathResult(rank, total, path.size(), initial, steps, current);
    }

    private static double round(double v) {
        return Math.round(v * 1_000_000d) / 1_000_000d;
    }

    /**
     * Cycle detection restricted to usable edges (three-color DFS). Returns one
     * representative vertex list per back edge found.
     */
    private List<CycleInfo> detectCycles(MigrationGraph graph, Set<Edge> usable) {
        Map<String, Integer> color = new LinkedHashMap<>(); // 0 white, 1 gray, 2 black
        graph.nodes().forEach(n -> color.put(n, 0));
        List<CycleInfo> cycles = new ArrayList<>();
        for (String node : graph.nodes()) {
            if (color.get(node) == 0) {
                cycleDfs(graph, usable, node, color, new ArrayList<>(), cycles);
            }
        }
        return cycles;
    }

    private void cycleDfs(MigrationGraph graph, Set<Edge> usable, String node,
                          Map<String, Integer> color, List<String> stack,
                          List<CycleInfo> cycles) {
        color.put(node, 1);
        stack.add(node);
        for (Edge edge : graph.outgoingFrom(node)) {
            if (!usable.contains(edge)) {
                continue;
            }
            String next = edge.to();
            if (color.get(next) == 1) {
                int start = stack.indexOf(next);
                List<String> ring = new ArrayList<>(stack.subList(start, stack.size()));
                ring.add(next);
                cycles.add(new CycleInfo(List.copyOf(ring)));
            } else if (color.get(next) == 0) {
                cycleDfs(graph, usable, next, color, stack, cycles);
            }
        }
        stack.remove(stack.size() - 1);
        color.put(node, 2);
    }
}
