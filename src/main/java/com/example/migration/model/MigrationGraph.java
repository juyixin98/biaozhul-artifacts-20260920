package com.example.migration.model;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Immutable directed graph of schema versions.
 *
 * <p>Node identifiers are opaque strings: their lexical or numeric form carries
 * NO meaning. Traversability is determined solely by declared edges.
 */
public final class MigrationGraph {

    private final Set<String> nodes;
    private final List<Edge> edges;
    private final Map<String, List<Edge>> outgoing;

    private MigrationGraph(Set<String> nodes, List<Edge> edges) {
        this.nodes = Collections.unmodifiableSet(new LinkedHashSet<>(nodes));
        this.edges = List.copyOf(edges);
        Map<String, List<Edge>> adj = new LinkedHashMap<>();
        this.nodes.forEach(n -> adj.put(n, new ArrayList<>()));
        for (Edge e : this.edges) {
            adj.get(e.from()).add(e);
        }
        adj.replaceAll((k, v) -> List.copyOf(v));
        this.outgoing = Collections.unmodifiableMap(adj);
    }

    public static MigrationGraph of(List<String> declaredNodes, List<Edge> edges) {
        Set<String> nodes = new LinkedHashSet<>();
        if (declaredNodes != null) {
            nodes.addAll(declaredNodes);
        }
        for (Edge e : edges) {
            nodes.add(e.from());
            nodes.add(e.to());
        }
        // duplicate parallel edges are allowed (different preconditions/windows);
        // exact duplicate (same endpoints) of identical shape is rejected.
        Set<String> seen = new LinkedHashSet<>();
        for (Edge e : edges) {
            String key = e.from() + "->" + e.to() + "#" + e.preconditions()
                    + "#" + e.validFrom() + "#" + e.validTo();
            if (!seen.add(key)) {
                throw new IllegalArgumentException("duplicate edge definition: " + e.from()
                        + "->" + e.to() + " with identical constraints");
            }
        }
        return new MigrationGraph(nodes, edges);
    }

    public Set<String> nodes() {
        return nodes;
    }

    public List<Edge> edges() {
        return edges;
    }

    public List<Edge> outgoingFrom(String node) {
        return outgoing.getOrDefault(node, List.of());
    }

    public boolean contains(String node) {
        return nodes.contains(node);
    }

    /** Real edge directed {@code from -> to} (any one). Precondition/window agnostic. */
    public boolean hasDirectedEdge(String from, String to) {
        return edges.stream().anyMatch(e -> e.from().equals(from) && e.to().equals(to));
    }
}
