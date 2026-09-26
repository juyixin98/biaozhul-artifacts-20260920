package com.migration.planner.graph;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.model.SchemaGraph;
import com.migration.planner.plan.PlanException;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class GraphLoaderTest {

    private final GraphLoader loader = new GraphLoader(new ObjectMapper());

    @Test
    void loadsDefaultFixture() {
        SchemaGraph graph = loader.loadDefault();
        assertEquals("schema-migration-fixture", graph.graphId());
        assertEquals(10, graph.nodes().size());
        assertEquals(13, graph.edges().size());
    }

    @Test
    void warnsWhenDeclaredReversibleHasNoInverseEdge() {
        SchemaGraph graph = loader.loadDefault();
        assertTrue(graph.warnings().stream().anyMatch(w -> w.contains("e10")),
                "expected a load warning about e10, got: " + graph.warnings());
    }

    @Test
    void rejectsDuplicateEdgeIds() {
        PlanException e = assertThrows(PlanException.class, () -> load("""
                {"nodes":["a","b"],"edges":[
                  {"id":"x","from":"a","to":"b","cost":1},
                  {"id":"x","from":"b","to":"a","cost":1}]}
                """));
        assertEquals(PlanException.Code.GRAPH_ERROR, e.code());
        assertTrue(e.getMessage().contains("duplicate"));
    }

    @Test
    void rejectsDanglingEndpoint() {
        PlanException e = assertThrows(PlanException.class, () -> load("""
                {"nodes":["a"],"edges":[{"id":"x","from":"a","to":"ghost","cost":1}]}
                """));
        assertEquals(PlanException.Code.GRAPH_ERROR, e.code());
    }

    @Test
    void rejectsNonPositiveCost() {
        PlanException e = assertThrows(PlanException.class, () -> load("""
                {"nodes":["a","b"],"edges":[{"id":"x","from":"a","to":"b","cost":0}]}
                """));
        assertEquals(PlanException.Code.GRAPH_ERROR, e.code());
        assertTrue(e.getMessage().contains("positive cost"));
    }

    private SchemaGraph load(String json) throws IOException {
        return loader.load(new ByteArrayInputStream(json.getBytes(StandardCharsets.UTF_8)));
    }
}
