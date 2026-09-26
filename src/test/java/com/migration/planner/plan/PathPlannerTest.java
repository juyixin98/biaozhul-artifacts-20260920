package com.migration.planner.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.graph.GraphLoader;
import com.migration.planner.model.SchemaGraph;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PathPlannerTest {

    private static SchemaGraph graph;
    private final PathPlanner planner = new PathPlanner();

    @BeforeAll
    static void loadGraph() {
        graph = new GraphLoader(new ObjectMapper()).loadDefault();
    }

    @Test
    void forkChoosesLowestCostBranch() {
        PathPlanner.SearchResult result = planner.findBestPaths(
                graph, "v1", "v3", Map.of("maintenance_window", true), 16);
        assertEquals(List.of("v1", "v2a", "v3"), result.chosen().nodes());
        assertEquals(4, result.chosen().totalCost());
    }

    @Test
    void unmetPreconditionExcludesEdge() {
        // Without maintenance_window, e4 (v2a->v3) is unusable; only the v2b branch remains.
        PathPlanner.SearchResult result = planner.findBestPaths(
                graph, "v1", "v3", Map.of(), 16);
        assertEquals(List.of("v1", "v2b", "v3"), result.chosen().nodes());
        assertEquals(6, result.chosen().totalCost());
    }

    @Test
    void cycleDoesNotTrapPlanner() {
        // The v3->v4->v5->v3 cycle must not cause loops or inflated costs.
        PathPlanner.SearchResult result = planner.findBestPaths(
                graph, "v3", "v6", Map.of("backup_verified", true), 16);
        assertEquals(7, result.chosen().totalCost());
        assertEquals(result.chosen().nodes().size(),
                result.chosen().nodes().stream().distinct().count(),
                "path must not revisit nodes: " + result.chosen().nodes());
    }

    @Test
    void costTieIsDeterministicAndReportsAlternatives() {
        Map<String, Boolean> ctx = Map.of("maintenance_window", true, "backup_verified", true);
        PathPlanner.SearchResult first = planner.findBestPaths(graph, "v1", "v6", ctx, 16);
        PathPlanner.SearchResult second = planner.findBestPaths(graph, "v1", "v6", ctx, 16);

        assertEquals(11, first.chosen().totalCost());
        assertEquals(2, first.alternatives().size(), "expected a three-way cost tie");
        assertEquals(List.of("v1", "v2a", "v3", "v4", "v5", "v6"), first.chosen().nodes());
        assertEquals(first.chosen().nodes(), second.chosen().nodes(),
                "tie-breaking must be deterministic across runs");
        assertTrue(first.alternatives().stream()
                        .allMatch(p -> p.totalCost() == first.chosen().totalCost()),
                "alternatives must all be equal-cost");
    }

    @Test
    void noPathIsReportedWithDiagnostics() {
        PlanException e = assertThrows(PlanException.class, () ->
                planner.findBestPaths(graph, "v6", "v1", Map.of(), 16));
        assertEquals(PlanException.Code.NO_PATH, e.code());
        assertTrue(e.details().containsKey("reachableVersions"));
        assertTrue(e.details().containsKey("edgesBlockedByPreconditions"));
    }

    @Test
    void isolatedNodeHasNoPath() {
        PlanException e = assertThrows(PlanException.class, () ->
                planner.findBestPaths(graph, "isolated", "v1", Map.of(), 16));
        assertEquals(PlanException.Code.NO_PATH, e.code());
    }

    @Test
    void unknownVersionIsRejected() {
        PlanException e = assertThrows(PlanException.class, () ->
                planner.findBestPaths(graph, "v9", "v1", Map.of(), 16));
        assertEquals(PlanException.Code.UNKNOWN_VERSION, e.code());
    }

    @Test
    void versionNumbersAreNotUsedToInferMigratability() {
        // Numerically "9 < 10", but the only real edge is schema-10 -> schema-9.
        PathPlanner.SearchResult down = planner.findBestPaths(
                graph, "schema-10", "schema-9", Map.of(), 16);
        assertEquals(List.of("schema-10", "schema-9"), down.chosen().nodes());

        PlanException up = assertThrows(PlanException.class, () ->
                planner.findBestPaths(graph, "schema-9", "schema-10", Map.of(), 16));
        assertEquals(PlanException.Code.NO_PATH, up.code(),
                "planner must not infer an upgrade path from version numbers");
    }

    @Test
    void sameSourceAndTargetIsTrivialPath() {
        PathPlanner.SearchResult result = planner.findBestPaths(graph, "v3", "v3", Map.of(), 16);
        assertEquals(0, result.chosen().totalCost());
        assertTrue(result.chosen().edges().isEmpty());
    }
}
