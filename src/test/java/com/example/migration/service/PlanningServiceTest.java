package com.example.migration.service;

import com.example.migration.data.DemoRequests;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Application-service level tests incl. environment metadata and error envelopes. */
class PlanningServiceTest {

    private final PlanningService service = new PlanningService("unit-test");

    @Test
    @DisplayName("response carries tzdb version and environment metadata")
    void environmentMetadata() {
        PlanResponse response = service.plan(DemoRequests.forScenario("tie"));
        assertTrue(response.success());
        assertEquals("FOUND", response.status());
        assertNotNull(response.environment().tzdbVersion());
        assertTrue(response.environment().tzdbVersion().matches("\\d{4}[a-z]"),
                "tzdb version should look like 2026b, got "
                        + response.environment().tzdbVersion());
        assertEquals("unit-test", response.environment().dataSet());
        assertNotNull(response.environment().javaVersion());
        assertEquals("UPGRADE", response.mode());
    }

    @Test
    @DisplayName("disconnected scenario -> NO_PATH, success=false")
    void noPathEnvelope() {
        PlanResponse response = service.plan(DemoRequests.forScenario("island"));
        assertFalse(response.success());
        assertEquals("NO_PATH", response.status());
        assertEquals(List.of(), response.paths());
    }

    @Test
    @DisplayName("unknown version id -> ERROR/UNKNOWN_VERSION, never inferred from numbers")
    void unknownVersion() {
        PlanRequest req = DemoRequests.forScenario("tie");
        PlanRequest bad = new PlanRequest(req.graph(), "a", "v999",
                req.at(), req.attributes(), req.mode(), req.requireReversible(), req.maxPaths());
        PlanResponse response = service.plan(bad);
        assertEquals("ERROR", response.status());
        assertEquals("UNKNOWN_VERSION", response.error().code());
        assertTrue(response.error().message().contains("v999"));
    }

    @Test
    @DisplayName("rollback demo response marks ROLLBACK mode")
    void rollbackMode() {
        PlanResponse response = service.plan(DemoRequests.forScenario("rollback"));
        assertEquals("ROLLBACK", response.mode());
        assertTrue(response.success());
        assertEquals(7, response.paths().get(0).totalCost(), 1e-9);
    }

    @Test
    @DisplayName("cycle demo carries a warning and still returns ranked paths")
    void cycleWarning() {
        PlanResponse response = service.plan(DemoRequests.forScenario("cycle"));
        assertTrue(response.success());
        assertTrue(response.warnings().stream().anyMatch(w -> w.contains("cycle")));
    }
}
