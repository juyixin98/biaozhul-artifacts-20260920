package com.migration.planner.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.graph.GraphLoader;
import com.migration.planner.model.Checkpoint;
import com.migration.planner.model.MigrationPlan;
import com.migration.planner.model.PlanRequest;
import com.migration.planner.plan.PlanException;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PlanServiceTest {

    private static PlanService service;

    @BeforeAll
    static void setUp() {
        service = new PlanService(new GraphLoader(new ObjectMapper()).loadDefault());
    }

    @Test
    void rollbackExistsOnlyWhenRealInverseEdgeExists() {
        // No maintenance_window: plan goes v1 -> v2b (e3, irreversible) -> v3 (e5, inverse e6).
        MigrationPlan plan = service.plan(new PlanRequest("v1", "v3", Map.of(), null));

        Checkpoint atV2b = plan.checkpoints().get(0);
        assertEquals("v2b", atV2b.atVersion());
        assertFalse(atV2b.rollback().available(),
                "e3 has no inverse edge, so rollback at v2b must be unavailable");

        Checkpoint atV3 = plan.checkpoints().get(1);
        assertEquals("v3", atV3.atVersion());
        assertTrue(atV3.rollback().available());
        assertEquals("e6", atV3.rollback().edgeId());
        assertEquals(1, atV3.rollback().cost());
    }

    @Test
    void declaredReversibleWithoutInverseEdgeIsNotRollbackable() {
        // Chosen path v3 -> v4 -> v6 ends with e10, which declares reversible=true
        // but has no real inverse edge in the graph.
        MigrationPlan plan = service.plan(new PlanRequest("v3", "v6", Map.of(), null));
        Checkpoint last = plan.checkpoints().get(plan.checkpoints().size() - 1);
        assertEquals("v6", last.atVersion());
        assertFalse(last.rollback().available(),
                "declared reversibility must not count as a rollback path");
        assertTrue(last.rollback().reason().contains("no real inverse edge"));
        assertTrue(plan.warnings().stream().anyMatch(w -> w.contains("e10")));
    }

    @Test
    void checkpointsTrackCumulativeCost() {
        MigrationPlan plan = service.plan(new PlanRequest("v1", "v3",
                Map.of("maintenance_window", true), null));
        assertEquals(2, plan.checkpoints().size());
        assertEquals(2, plan.checkpoints().get(0).cumulativeCost());
        assertEquals(4, plan.checkpoints().get(1).cumulativeCost());
        assertEquals(4, plan.totalCost());
    }

    @Test
    void costTieProducesAlternativesAndDeterministicChoice() {
        Map<String, Boolean> ctx = Map.of("maintenance_window", true, "backup_verified", true);
        MigrationPlan plan = service.plan(new PlanRequest("v1", "v6", ctx, null));
        assertEquals(11, plan.totalCost());
        assertTrue(plan.tieBrokenDeterministically());
        assertEquals(2, plan.alternatives().size());
        assertTrue(plan.alternatives().stream().allMatch(a -> a.totalCost() == 11));
    }

    @Test
    void planCarriesTzdbAndGraphMetadata() {
        MigrationPlan plan = service.plan(new PlanRequest("v1", "v3", Map.of(), null));
        assertTrue(plan.ok());
        assertEquals("schema-migration-fixture", plan.graphId());
        assertTrue(plan.tzdbVersion().matches("\\d{4}[a-z]"),
                "unexpected tzdb version format: " + plan.tzdbVersion());
    }

    @Test
    void invalidRequestsAreRejected() {
        PlanException missing = assertThrows(PlanException.class,
                () -> service.plan(new PlanRequest(null, "v1", Map.of(), null)));
        assertEquals(PlanException.Code.INVALID_REQUEST, missing.code());

        PlanException badCap = assertThrows(PlanException.class,
                () -> service.plan(new PlanRequest("v1", "v3", Map.of(), 0)));
        assertEquals(PlanException.Code.INVALID_REQUEST, badCap.code());
    }
}
