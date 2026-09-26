package com.migration.planner.model;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * Immutable directed graph of schema versions. Node ids are opaque strings:
 * no ordering or migratability is ever inferred from version numbers.
 */
public final class SchemaGraph {

    private final String graphId;
    private final String graphVersion;
    private final SortedSet<String> nodes;
    private final List<MigrationEdge> edges;
    private final Map<String, List<MigrationEdge>> outgoing;
    private final Map<String, List<MigrationEdge>> incoming;
    private final List<String> warnings;

    public SchemaGraph(String graphId, String graphVersion, Set<String> nodes, List<MigrationEdge> edges) {
        this.graphId = graphId;
        this.graphVersion = graphVersion;
        this.nodes = Collections.unmodifiableSortedSet(new TreeSet<>(nodes));
        this.edges = List.copyOf(edges);
        Map<String, List<MigrationEdge>> out = new LinkedHashMap<>();
        Map<String, List<MigrationEdge>> in = new LinkedHashMap<>();
        for (String n : this.nodes) {
            out.put(n, new ArrayList<>());
            in.put(n, new ArrayList<>());
        }
        for (MigrationEdge e : this.edges) {
            out.get(e.from()).add(e);
            in.get(e.to()).add(e);
        }
        Comparator<MigrationEdge> byId = Comparator.comparing(MigrationEdge::id);
        this.outgoing = freeze(out, byId);
        this.incoming = freeze(in, byId);
        this.warnings = collectWarnings();
    }

    private static Map<String, List<MigrationEdge>> freeze(
            Map<String, List<MigrationEdge>> adjacency, Comparator<MigrationEdge> order) {
        Map<String, List<MigrationEdge>> frozen = new LinkedHashMap<>();
        adjacency.forEach((node, list) -> {
            list.sort(order);
            frozen.put(node, List.copyOf(list));
        });
        return Collections.unmodifiableMap(frozen);
    }

    private List<String> collectWarnings() {
        List<String> result = new ArrayList<>();
        for (MigrationEdge e : edges) {
            if (e.reversible() && findInverse(e).isEmpty()) {
                result.add("edge " + e.id() + " (" + e.from() + "->" + e.to()
                        + ") declares reversible=true but no real inverse edge exists; rollback unavailable");
            }
        }
        return List.copyOf(result);
    }

    /** The real inverse edge of {@code e}, if one exists: lowest cost, then lowest id. */
    public Optional<MigrationEdge> findInverse(MigrationEdge e) {
        return outgoing.getOrDefault(e.to(), List.of()).stream()
                .filter(candidate -> candidate.from().equals(e.to()) && candidate.to().equals(e.from()))
                .min(Comparator.comparingLong(MigrationEdge::cost).thenComparing(MigrationEdge::id));
    }

    public String graphId() {
        return graphId;
    }

    public String graphVersion() {
        return graphVersion;
    }

    public SortedSet<String> nodes() {
        return nodes;
    }

    public List<MigrationEdge> edges() {
        return edges;
    }

    public List<MigrationEdge> outgoingFrom(String node) {
        return outgoing.getOrDefault(node, List.of());
    }

    public List<MigrationEdge> incomingTo(String node) {
        return incoming.getOrDefault(node, List.of());
    }

    public List<String> warnings() {
        return warnings;
    }
}
