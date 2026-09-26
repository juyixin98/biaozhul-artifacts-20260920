package com.example.migration.data;

import com.example.migration.model.PlanRequest;

import java.time.Instant;
import java.util.Map;

/**
 * Builds canonical {@link PlanRequest}s over {@link FixedData} scenarios so the
 * CLI demo and tests share one source of truth for "interesting" inputs.
 */
public final class DemoRequests {

    public static final Instant WINDOW_INSIDE = Instant.parse("2026-06-01T02:00:00Z");
    public static final Instant WINDOW_BEFORE = Instant.parse("2025-06-01T02:00:00Z");

    private DemoRequests() {
    }

    public static PlanRequest forScenario(String scenario) {
        FixedData.Scenario s = FixedData.byName(scenario);
        PlanRequest.Graph g = new PlanRequest.Graph(s.nodes(), s.edges());
        return switch (scenario) {
            case "shop" -> new PlanRequest(g, "legacy", "schema-aurora", WINDOW_INSIDE,
                    Map.of("maintenance_window", true, "region", "cn-east", "engine", "aurora"),
                    PlanRequest.Mode.UPGRADE, false, 3);
            case "branch" -> new PlanRequest(g, "v2", "v4", null, Map.of(),
                    PlanRequest.Mode.UPGRADE, null, 3);
            case "cycle" -> new PlanRequest(g, "v2", "v4", null, Map.of(),
                    PlanRequest.Mode.UPGRADE, null, 5);
            case "island" -> new PlanRequest(g, "v2", "vX-orphan", null, Map.of(),
                    PlanRequest.Mode.UPGRADE, null, 3);
            case "tie" -> new PlanRequest(g, "a", "d", null, Map.of(),
                    PlanRequest.Mode.UPGRADE, null, 3);
            case "rollback" -> new PlanRequest(g, "schema-aurora", "v2", null, Map.of(),
                    PlanRequest.Mode.ROLLBACK, false, 3);
            default -> throw new IllegalArgumentException("no demo request for " + scenario);
        };
    }
}
