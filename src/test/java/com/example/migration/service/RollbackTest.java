package com.example.migration.service;

import com.example.migration.model.Edge;
import com.example.migration.model.MigrationGraph;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse.PathResult;
import com.example.migration.model.PlanResponse.StepResult;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static com.example.migration.service.PlannerFixtures.request;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Rollback correctness: a {@code reversible=true} flag is NOT an edge.
 * Rollback is possible iff a real directed reverse edge (or path of them)
 * exists and satisfies all constraints.
 */
class RollbackTest {

    @Test
    @DisplayName("rollback uses only the real reverse chain; hotfix line cannot roll back")
    void rollbackRequiresRealReverseEdges() {
        var plan = PlannerFixtures.runDemo("rollback");
        // schema-aurora -> v3-1 -> v2 exists as real edges (cost 4+3=7)
        assertEquals(1, plan.paths().size(), "exactly one real rollback path");
        PathResult p = plan.paths().get(0);
        PlannerFixtures.assertPath(p, 7, List.of("schema-aurora", "v3-1", "v2"));
        for (StepResult s : p.steps()) {
            assertEquals("MIGRATE", s.action());
        }
    }

    @Test
    @DisplayName("reversible=true without a reverse edge does NOT enable rollback")
    void reversibleFlagIsNotAnEdge() {
        // single forward edge u->w marked reversible, no reverse edge at all
        List<Edge> edges = List.of(Edge.of("u", "w", 3, true));
        MigrationGraph g = MigrationGraph.of(null, edges);
        var plan = new PathPlanner().plan(g,
                request(g, "w", "u", PlanRequest.Mode.ROLLBACK, Map.of(),
                        Instant.parse("2026-06-01T00:00:00Z"), false, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertTrue(plan.paths().isEmpty(),
                "rollback must be impossible despite reversible=true");
    }

    @Test
    @DisplayName("rollback step metadata reports whether a real reverse edge exists")
    void reverseEdgeMetadata() {
        // u->w forward (marked reversible) and w->u real reverse edge
        List<Edge> edges = List.of(Edge.of("u", "w", 3, true), Edge.of("w", "u", 9, false));
        MigrationGraph g = MigrationGraph.of(null, edges);
        var plan = new PathPlanner().plan(g,
                request(g, "w", "u", PlanRequest.Mode.ROLLBACK, Map.of(),
                        Instant.parse("2026-06-01T00:00:00Z"), false, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertEquals(1, plan.paths().size());
        StepResult s = plan.paths().get(0).steps().get(0);
        // traversing the real edge w->u; the opposite direction u->w also exists
        assertTrue(s.reverseEdgeExists());
        assertTrue(s.reverseEdgeMarkedReversible());
    }

    @Test
    @DisplayName("strict rollback guard requires the forward counterpart marked reversible=true")
    void strictRollbackGuard() {
        // real reverse edge w->u exists, but forward u->w is marked irreversible
        List<Edge> edges = List.of(Edge.of("u", "w", 3, false), Edge.of("w", "u", 9, false));
        MigrationGraph g = MigrationGraph.of(null, edges);
        Instant at = Instant.parse("2026-06-01T00:00:00Z");

        var strict = new PathPlanner().plan(g,
                request(g, "w", "u", PlanRequest.Mode.ROLLBACK, Map.of(), at, true, 3), at);
        assertTrue(strict.paths().isEmpty(), "strict guard must block unmarked counterpart");
        assertTrue(strict.blockedEdges().stream()
                .flatMap(b -> b.reasons().stream())
                .anyMatch(r -> r.contains("not marked reversible=true")));

        var relaxed = new PathPlanner().plan(g,
                request(g, "w", "u", PlanRequest.Mode.ROLLBACK, Map.of(), at, false, 3), at);
        assertEquals(1, relaxed.paths().size(), "real reverse edge still traversable without guard");
    }

    @Test
    @DisplayName("forward upgrade is never blocked merely because no reverse edge exists")
    void upgradeDoesNotDemandReverseEdge() {
        // v9 looks "higher" than v1 but ids are opaque; a plain forward edge works.
        List<Edge> edges = List.of(Edge.of("v9", "v1", 2, false));
        MigrationGraph g = MigrationGraph.of(null, edges);
        var plan = new PathPlanner().plan(g,
                request(g, "v9", "v1", PlanRequest.Mode.UPGRADE, Map.of(),
                        Instant.parse("2026-06-01T00:00:00Z"), false, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertEquals(1, plan.paths().size());
        StepResult s = plan.paths().get(0).steps().get(0);
        assertFalse(s.reverseEdgeExists(), "one-way edge must report no reverse edge");
    }
}
