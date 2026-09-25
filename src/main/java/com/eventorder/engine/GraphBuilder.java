package com.eventorder.engine;

import com.eventorder.model.DependencyInput;
import com.eventorder.model.Edge;
import com.eventorder.model.EventInput;
import com.eventorder.model.OrderRequest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Turns a validated request into a directed graph of ordering edges.
 *
 * Rules:
 * <ul>
 *   <li>EXPLICIT — each declared dependency {@code before -> after}.</li>
 *   <li>VERSION — within one stream, lower version orders before higher version.</li>
 *   <li>TIME — if interval A ends at or before interval B starts, A orders before B.
 *       Overlapping intervals create NO edge: overlap never implies causality.</li>
 * </ul>
 */
public final class GraphBuilder {

    /** Immutable graph: sorted node ids plus representative edges per (from,to) pair. */
    public record BuiltGraph(List<String> nodeIds, List<Edge> edges,
                             Map<String, List<Edge>> adjacency) {
    }

    private GraphBuilder() {
    }

    public static BuiltGraph build(OrderRequest request, List<String> errors) {
        Map<String, EventInput> byId = validateEvents(request.events(), errors);
        Map<String, Interval> intervals = parseIntervals(request.events(), errors);
        validateVersions(request.events(), errors);

        List<Edge> edges = new ArrayList<>();
        edges.addAll(explicitEdges(request.dependencies(), byId, errors));
        edges.addAll(versionEdges(request.events()));
        edges.addAll(timeEdges(request.events(), intervals));

        List<String> nodeIds = byId.keySet().stream().sorted().toList();
        return new BuiltGraph(nodeIds, List.copyOf(edges), buildAdjacency(nodeIds, edges));
    }

    private record Interval(Instant start, Instant end) {
    }

    private static Map<String, EventInput> validateEvents(List<EventInput> events, List<String> errors) {
        Map<String, EventInput> byId = new LinkedHashMap<>();
        for (EventInput e : events) {
            if (e == null || e.id() == null || e.id().isBlank()) {
                errors.add("event with missing or blank id");
                continue;
            }
            if (byId.putIfAbsent(e.id(), e) != null) {
                errors.add("duplicate event id: '" + e.id() + "'");
            }
        }
        return byId;
    }

    private static Map<String, Interval> parseIntervals(List<EventInput> events, List<String> errors) {
        Map<String, Interval> intervals = new HashMap<>();
        for (EventInput e : events) {
            if (e == null || e.id() == null || e.interval() == null) {
                continue;
            }
            String id = e.id();
            try {
                Instant start = TimeParser.parse(e.interval().start());
                Instant end = TimeParser.parse(e.interval().end());
                if (end.isBefore(start)) {
                    errors.add("event '" + id + "': interval end is before start");
                    continue;
                }
                intervals.put(id, new Interval(start, end));
            } catch (IllegalArgumentException ex) {
                errors.add("event '" + id + "': " + ex.getMessage());
            }
        }
        return intervals;
    }

    private static void validateVersions(List<EventInput> events, List<String> errors) {
        Map<String, Map<Long, String>> seen = new HashMap<>();
        for (EventInput e : events) {
            if (e == null || e.id() == null || e.version() == null) {
                continue;
            }
            if (e.version().stream() == null || e.version().stream().isBlank()) {
                errors.add("event '" + e.id() + "': version stream is blank");
                continue;
            }
            String previous = seen.computeIfAbsent(e.version().stream(), k -> new HashMap<>())
                    .putIfAbsent(e.version().value(), e.id());
            if (previous != null && !previous.equals(e.id())) {
                errors.add("events '" + previous + "' and '" + e.id() + "' share version "
                        + e.version().value() + " on stream '" + e.version().stream() + "'");
            }
        }
    }

    private static List<Edge> explicitEdges(List<DependencyInput> dependencies,
                                            Map<String, EventInput> byId,
                                            List<String> errors) {
        List<Edge> edges = new ArrayList<>();
        for (DependencyInput d : dependencies) {
            if (d == null || d.before() == null || d.after() == null) {
                errors.add("dependency with missing 'before' or 'after'");
                continue;
            }
            if (!byId.containsKey(d.before())) {
                errors.add("dependency references unknown event: '" + d.before() + "'");
                continue;
            }
            if (!byId.containsKey(d.after())) {
                errors.add("dependency references unknown event: '" + d.after() + "'");
                continue;
            }
            String reason = d.reason() == null || d.reason().isBlank()
                    ? "explicit dependency: " + d.before() + " before " + d.after()
                    : "explicit dependency: " + d.reason();
            edges.add(new Edge(d.before(), d.after(), Edge.Kind.EXPLICIT, reason));
        }
        return edges;
    }

    private static List<Edge> versionEdges(List<EventInput> events) {
        Map<String, List<EventInput>> byStream = new TreeMap<>();
        for (EventInput e : events) {
            if (e != null && e.id() != null && e.version() != null
                    && e.version().stream() != null && !e.version().stream().isBlank()) {
                byStream.computeIfAbsent(e.version().stream(), k -> new ArrayList<>()).add(e);
            }
        }
        List<Edge> edges = new ArrayList<>();
        for (Map.Entry<String, List<EventInput>> entry : byStream.entrySet()) {
            List<EventInput> sorted = entry.getValue().stream()
                    .sorted(Comparator.comparingLong(e -> e.version().value()))
                    .toList();
            for (int i = 0; i + 1 < sorted.size(); i++) {
                EventInput lower = sorted.get(i);
                EventInput higher = sorted.get(i + 1);
                edges.add(new Edge(lower.id(), higher.id(), Edge.Kind.VERSION,
                        "version rule: " + lower.id() + " (v" + lower.version().value() + ") before "
                                + higher.id() + " (v" + higher.version().value() + ") on stream '"
                                + entry.getKey() + "'"));
            }
        }
        return edges;
    }

    private static List<Edge> timeEdges(List<EventInput> events, Map<String, Interval> intervals) {
        List<String> timedIds = events.stream()
                .filter(e -> e != null && e.id() != null && intervals.containsKey(e.id()))
                .map(EventInput::id)
                .sorted()
                .toList();
        List<Edge> edges = new ArrayList<>();
        for (int i = 0; i < timedIds.size(); i++) {
            for (int j = i + 1; j < timedIds.size(); j++) {
                Interval a = intervals.get(timedIds.get(i));
                Interval b = intervals.get(timedIds.get(j));
                if (!a.end().isAfter(b.start())) {
                    edges.add(timeEdge(timedIds.get(i), a, timedIds.get(j), b));
                } else if (!b.end().isAfter(a.start())) {
                    edges.add(timeEdge(timedIds.get(j), b, timedIds.get(i), a));
                }
                // Overlapping intervals: deliberately no edge — overlap is not causality.
            }
        }
        return edges;
    }

    private static Edge timeEdge(String firstId, Interval first, String secondId, Interval second) {
        return new Edge(firstId, secondId, Edge.Kind.TIME,
                "time rule: " + firstId + " ends " + TimeParser.formatUtc(first.end())
                        + " <= " + secondId + " starts " + TimeParser.formatUtc(second.start()));
    }

    /**
     * Adjacency with one representative edge per (from,to) pair, preferring
     * EXPLICIT over VERSION over TIME so conflict chains cite the strongest rule.
     * Neighbour lists are sorted for deterministic traversal.
     */
    private static Map<String, List<Edge>> buildAdjacency(List<String> nodeIds, List<Edge> edges) {
        Map<String, Map<String, Edge>> representative = new HashMap<>();
        for (Edge edge : edges) {
            representative.computeIfAbsent(edge.from(), k -> new HashMap<>())
                    .merge(edge.to(), edge, (existing, candidate) ->
                            candidate.kind().ordinal() < existing.kind().ordinal() ? candidate : existing);
        }
        Map<String, List<Edge>> adjacency = new HashMap<>();
        for (String id : nodeIds) {
            List<Edge> neighbours = representative.getOrDefault(id, Map.of()).values().stream()
                    .sorted(Comparator.comparing(Edge::to).thenComparing(e -> e.kind().name()))
                    .toList();
            adjacency.put(id, neighbours);
        }
        return adjacency;
    }
}
