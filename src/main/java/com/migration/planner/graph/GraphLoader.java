package com.migration.planner.graph;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.model.MigrationEdge;
import com.migration.planner.model.Precondition;
import com.migration.planner.model.SchemaGraph;
import com.migration.planner.plan.PlanException;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Loads and validates the fixed local graph fixture (JSON). */
public final class GraphLoader {

    public static final String DEFAULT_RESOURCE = "/graph.json";

    record GraphDoc(String graphId, String graphVersion, List<String> nodes, List<EdgeDoc> edges) {
    }

    record EdgeDoc(String id, String from, String to, Long cost, Boolean reversible,
                   String description, List<Precondition> preconditions) {
    }

    private final ObjectMapper mapper;

    public GraphLoader(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    public SchemaGraph loadDefault() {
        InputStream in = GraphLoader.class.getResourceAsStream(DEFAULT_RESOURCE);
        if (in == null) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "classpath resource not found: " + DEFAULT_RESOURCE);
        }
        try (in) {
            return load(in);
        } catch (IOException e) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "failed to read graph resource: " + e.getMessage());
        }
    }

    public SchemaGraph load(Path file) {
        try (InputStream in = Files.newInputStream(file)) {
            return load(in);
        } catch (IOException e) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "failed to read graph file " + file + ": " + e.getMessage());
        }
    }

    public SchemaGraph load(InputStream in) throws IOException {
        GraphDoc doc = mapper.readValue(in, GraphDoc.class);
        return build(doc);
    }

    private SchemaGraph build(GraphDoc doc) {
        if (doc == null || doc.nodes() == null || doc.edges() == null) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "graph document must contain 'nodes' and 'edges'");
        }
        Set<String> nodes = new HashSet<>(doc.nodes());
        Set<String> edgeIds = new HashSet<>();
        List<MigrationEdge> edges = doc.edges().stream().map(e -> toEdge(e, nodes, edgeIds)).toList();
        return new SchemaGraph(
                doc.graphId() == null ? "unnamed" : doc.graphId(),
                doc.graphVersion() == null ? "0" : doc.graphVersion(),
                nodes, edges);
    }

    private MigrationEdge toEdge(EdgeDoc doc, Set<String> nodes, Set<String> edgeIds) {
        if (doc.id() == null || doc.id().isBlank()) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR, "edge id must be non-empty");
        }
        if (!edgeIds.add(doc.id())) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR, "duplicate edge id: " + doc.id());
        }
        if (doc.from() == null || !nodes.contains(doc.from())) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "edge " + doc.id() + " has unknown 'from' node: " + doc.from());
        }
        if (doc.to() == null || !nodes.contains(doc.to())) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "edge " + doc.id() + " has unknown 'to' node: " + doc.to());
        }
        long cost = doc.cost() == null ? -1 : doc.cost();
        if (cost <= 0) {
            throw new PlanException(PlanException.Code.GRAPH_ERROR,
                    "edge " + doc.id() + " must have a positive cost, got: " + cost);
        }
        return new MigrationEdge(doc.id(), doc.from(), doc.to(), cost,
                Boolean.TRUE.equals(doc.reversible()), doc.description(), doc.preconditions());
    }
}
