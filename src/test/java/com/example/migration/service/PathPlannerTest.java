package com.example.migration.service;

import com.example.migration.data.FixedData;
import com.example.migration.model.Edge;
import com.example.migration.model.MigrationGraph;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse.BlockedEdge;
import com.example.migration.model.PlanResponse.PathResult;

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
 * Acceptance scenarios: forks, cycles, no-path and cost ties — plus
 * preconditions, time windows and deterministic ranking.
 */
class PathPlannerTest {

    @Test
    @DisplayName("fork: cheapest of multiple branches is rank 1, alternatives follow")
    void forkPicksCheapestAndKeepsAlternatives() {
        // custom request that ALSO satisfies the hotfix edge precondition
        FixedData.Scenario s0 = FixedData.byName("shop");
        MigrationGraph g0 = MigrationGraph.of(s0.nodes(), s0.edges());
        Instant at = Instant.parse("2026-06-01T00:00:00Z");
        PlanRequest forkReq = request(g0, "legacy", "schema-aurora", PlanRequest.Mode.UPGRADE,
                Map.of("maintenance_window", true, "region", "cn-east",
                        "engine", "aurora", "emergency", true),
                at, false, 5);
        var plan = new PathPlanner().plan(g0, forkReq, at);
        assertEquals(2, plan.paths().size(), "both fork lines must be enumerated");

        PathResult best = plan.paths().get(0);
        // cheapest: legacy->v2(5) -> v3-1(3) -> aurora(4) = 12 (hotfix line is 13)
        PlannerFixtures.assertPath(best, 12,
                List.of("legacy", "v2", "v3-1", "schema-aurora"));

        // the fork alternative is present as a later rank
        List<List<String>> all = plan.paths().stream().map(PlannerFixtures::vertexSequence).toList();
        assertTrue(all.contains(List.of("legacy", "v2", "v3-hotfix", "schema-aurora")),
                "hotfix fork must be enumerated, got " + all);
        // cost-sorted
        for (int i = 1; i < plan.paths().size(); i++) {
            assertTrue(plan.paths().get(i - 1).totalCost() <= plan.paths().get(i).totalCost());
        }
    }

    @Test
    @DisplayName("fork without the emergency attribute: hotfix edge is reported blocked")
    void forkBlockedByPreconditionInDemo() {
        var plan = PlannerFixtures.runDemo("shop");
        assertEquals(1, plan.paths().size());
        PlannerFixtures.assertPath(plan.paths().get(0), 12,
                List.of("legacy", "v2", "v3-1", "schema-aurora"));
        assertTrue(plan.blockedEdges().stream()
                .anyMatch(b -> b.from().equals("v2") && b.to().equals("v3-hotfix")
                        && b.reasons().stream().anyMatch(r -> r.contains("emergency"))));
    }

    @Test
    @DisplayName("fork: diamond graph enumerates exactly the simple paths, both branches")
    void diamondFork() {
        var plan = PlannerFixtures.runDemo("branch");
        assertEquals(3, plan.paths().size()); // via blue, via green, blue->green cross edge
        List<List<String>> all = plan.paths().stream().map(PlannerFixtures::vertexSequence).toList();
        assertTrue(all.contains(List.of("v2", "v3-blue", "v4")));
        assertTrue(all.contains(List.of("v2", "v3-green", "v4")));
        assertTrue(all.contains(List.of("v2", "v3-blue", "v3-green", "v4")));
    }

    @Test
    @DisplayName("cycle: enumeration terminates, emits only simple paths, warns about the cycle")
    void cycleTerminatesAndEmitsSimplePathsOnly() {
        var plan = PlannerFixtures.runDemo("cycle");
        assertFalse(plan.paths().isEmpty());
        // all simple paths v2 -> v4 given edges:
        //   v2-v3-v4
        //   v2-v3-v3b-v4
        //   v2-v3-v3b-(back to v3) prohibited
        List<List<String>> all = plan.paths().stream().map(PlannerFixtures::vertexSequence).toList();
        assertEquals(2, all.size(), "exactly two simple paths, got " + all);
        assertTrue(all.contains(List.of("v2", "v3", "v4")));
        assertTrue(all.contains(List.of("v2", "v3", "v3b", "v4")));
        for (List<String> path : all) {
            assertEquals(path.size(), path.stream().distinct().count(),
                    "path revisits a vertex: " + path);
        }
        assertTrue(plan.warnings().stream().anyMatch(w -> w.contains("cycle")),
                "a cycle warning must be emitted: " + plan.warnings());
        assertEquals(1, plan.cycles().size());
        assertEquals(List.of("v3", "v3b", "v3"), plan.cycles().get(0).vertices());
    }

    @Test
    @DisplayName("no path: disconnected target yields NO_PATH with blocked diagnostics")
    void noPathWhenDisconnected() {
        var plan = PlannerFixtures.runDemo("island");
        assertTrue(plan.paths().isEmpty());
    }

    @Test
    @DisplayName("cost tie: equal-cost paths are BOTH returned with stable lexical order")
    void exactCostTieReturnsBothPaths() {
        var first = PlannerFixtures.runDemo("tie");
        var second = PlannerFixtures.runDemo("tie"); // determinism across runs
        assertEquals(2, first.paths().size());
        assertEquals(5, first.paths().get(0).totalCost(), 1e-9);
        assertEquals(5, first.paths().get(1).totalCost(), 1e-9);
        assertEquals(List.of("a", "b", "d"),
                PlannerFixtures.vertexSequence(first.paths().get(0)),
                "lexicographically smaller signature ranks first");
        assertEquals(List.of("a", "c", "d"),
                PlannerFixtures.vertexSequence(first.paths().get(1)));
        assertEquals(
                first.paths().stream().map(PlannerFixtures::vertexSequence).toList(),
                second.paths().stream().map(PlannerFixtures::vertexSequence).toList(),
                "ranking must be deterministic");
    }

    @Test
    @DisplayName("precondition: missing/unsatisfied attributes block the edge with reasons")
    void preconditionBlocksEdge() {
        List<Edge> edges = List.of(
                new Edge("s", "t", 1, true,
                        Map.of("region", List.of("cn-east", "cn-north")),
                        null, null, null));
        MigrationGraph g = MigrationGraph.of(null, edges);

        var bad = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE,
                        Map.of("region", "eu-west"), Instant.parse("2026-06-01T00:00:00Z"),
                        null, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertTrue(bad.paths().isEmpty());
        BlockedEdge blocked = bad.blockedEdges().get(0);
        assertTrue(blocked.reasons().get(0).contains("region"));

        var missing = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE, Map.of(),
                        Instant.parse("2026-06-01T00:00:00Z"), null, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertTrue(missing.paths().isEmpty());
        assertTrue(missing.blockedEdges().get(0).reasons().get(0).contains("missing"));

        var ok = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE,
                        Map.of("region", "cn-north"), Instant.parse("2026-06-01T00:00:00Z"),
                        null, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertEquals(1, ok.paths().size());
    }

    @Test
    @DisplayName("time window: edge blocked before validFrom and usable inside window")
    void timeWindowRules() {
        List<Edge> edges = List.of(new Edge("s", "t", 1, true, Map.of(),
                Instant.parse("2026-01-01T00:00:00Z"),
                Instant.parse("2026-12-31T23:59:59Z"), null));
        MigrationGraph g = MigrationGraph.of(null, edges);

        var before = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE, Map.of(),
                        Instant.parse("2025-06-01T00:00:00Z"), null, 3),
                Instant.parse("2025-06-01T00:00:00Z"));
        assertTrue(before.paths().isEmpty());
        assertTrue(before.blockedEdges().get(0).reasons().stream()
                .anyMatch(r -> r.contains("not open until")));

        var inside = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE, Map.of(),
                        Instant.parse("2026-06-01T00:00:00Z"), null, 3),
                Instant.parse("2026-06-01T00:00:00Z"));
        assertEquals(1, inside.paths().size());

        var after = new PathPlanner().plan(g,
                request(g, "s", "t", PlanRequest.Mode.UPGRADE, Map.of(),
                        Instant.parse("2027-06-01T00:00:00Z"), null, 3),
                Instant.parse("2027-06-01T00:00:00Z"));
        assertTrue(after.paths().isEmpty());
        assertTrue(after.blockedEdges().get(0).reasons().stream()
                .anyMatch(r -> r.contains("closed after")));
    }

    @Test
    @DisplayName("every emitted step cites a real declared edge and carries checkpoints")
    void stepsAndCheckpointsReferenceRealEdges() {
        var plan = PlannerFixtures.runDemo("shop");
        FixedData.Scenario s = FixedData.byName("shop");
        MigrationGraph g = MigrationGraph.of(s.nodes(), s.edges());
        for (PathResult p : plan.paths()) {
            PlannerFixtures.assertEveryStepCitesRealEdge(p, g);
            assertEquals("CP-0", p.initialCheckpoint().id());
            assertEquals("CP-" + p.stepCount(), p.finalCheckpoint().id());
            assertEquals(p.totalCost(), p.finalCheckpoint().cumulativeCost(), 1e-9);
        }
    }

    @Test
    @DisplayName("requireReversible guard excludes edges marked reversible=false (UPGRADE)")
    void requireReversibleGuard() {
        // start at v2: v2->v3-1 is reversible=true, v2->v3-hotfix is false
        FixedData.Scenario s = FixedData.byName("shop");
        MigrationGraph g = MigrationGraph.of(s.nodes(), s.edges());
        Instant at = Instant.parse("2026-06-01T00:00:00Z");
        PlanRequest req = request(g, "v2", "schema-aurora", PlanRequest.Mode.UPGRADE,
                Map.of("region", "cn-east", "engine", "aurora", "emergency", true),
                at, true, 5);
        var plan = new PathPlanner().plan(g, req, at);
        List<List<String>> all = plan.paths().stream().map(PlannerFixtures::vertexSequence).toList();
        assertTrue(all.contains(List.of("v2", "v3-1", "schema-aurora")));
        assertFalse(all.stream().anyMatch(p -> p.contains("v3-hotfix")),
                "irreversible edge must be excluded under the guard, got " + all);
    }
}
