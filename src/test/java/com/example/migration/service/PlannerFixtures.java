package com.example.migration.service;

import com.example.migration.data.DemoRequests;
import com.example.migration.data.FixedData;
import com.example.migration.model.Edge;
import com.example.migration.model.MigrationGraph;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse.PathResult;
import com.example.migration.model.PlanResponse.StepResult;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Shared builders for planner tests. */
final class PlannerFixtures {

    private PlannerFixtures() {
    }

    static PlanRequest request(MigrationGraph g, String from, String to,
                               PlanRequest.Mode mode, Map<String, Object> attrs,
                               Instant at, Boolean requireReversible, int maxPaths) {
        return new PlanRequest(new PlanRequest.Graph(List.copyOf(g.nodes()), g.edges()),
                from, to, at, attrs, mode, requireReversible, maxPaths);
    }

    static PlanRequest simple(String from, String to, List<Edge> edges) {
        MigrationGraph g = MigrationGraph.of(null, edges);
        return request(g, from, to, PlanRequest.Mode.UPGRADE, Map.of(), null, null, 10);
    }

    static PathPlanner.Plan runDemo(String scenario) {
        FixedData.Scenario s = FixedData.byName(scenario);
        MigrationGraph g = MigrationGraph.of(s.nodes(), s.edges());
        PlanRequest req = DemoRequests.forScenario(scenario);
        return new PathPlanner().plan(g, req,
                req.at() == null ? Instant.parse("2026-06-01T00:00:00Z") : req.at());
    }

    static List<String> vertexSequence(PathResult p) {
        List<String> seq = new java.util.ArrayList<>();
        seq.add(p.initialCheckpoint().version());
        for (StepResult s : p.steps()) {
            seq.add(s.to());
        }
        return seq;
    }

    static void assertPath(PathResult p, double cost, List<String> vertices) {
        assertEquals(cost, p.totalCost(), 1e-9, "total cost");
        assertEquals(vertices, vertexSequence(p), "vertex sequence");
        // checkpoints: one per step plus initial, monotonic cumulative cost
        assertEquals(vertices.size() - 1, p.steps().size());
        assertEquals(0, p.initialCheckpoint().cumulativeCost(), 1e-9);
        for (StepResult s : p.steps()) {
            assertEquals(s.checkpoint().version(), s.to());
            assertEquals(s.checkpoint().cumulativeCost(),
                    p.steps().subList(0, s.sequence()).stream()
                            .mapToDouble(StepResult::cost).sum(), 1e-9);
            assertEquals("CP-" + s.sequence(), s.checkpoint().id());
        }
        assertTrue(p.finalCheckpoint().note().contains(vertices.get(vertices.size() - 1)));
    }

    static void assertEveryStepCitesRealEdge(PathResult p, MigrationGraph g) {
        for (StepResult s : p.steps()) {
            assertTrue(g.hasDirectedEdge(s.from(), s.to()),
                    "step cites a non-existent edge: " + s.from() + "->" + s.to());
            assertFalse(s.action() == null || s.action().isBlank());
        }
    }
}
