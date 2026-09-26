package com.example.migration.service;

import com.example.migration.data.FixedData;
import com.example.migration.model.MigrationGraph;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse;
import com.example.migration.model.PlanResponse.ApiError;
import com.example.migration.model.PlanResponse.EnvironmentInfo;

import java.time.Instant;
import java.util.List;

/**
 * Application service: validates the request, runs the planner and assembles
 * the {@link PlanResponse}, including tzdb environment metadata.
 */
public final class PlanningService {

    private final String dataSetName;

    public PlanningService(String dataSetName) {
        this.dataSetName = dataSetName == null ? FixedData.DEFAULT_SCENARIO : dataSetName;
    }

    public PlanResponse plan(PlanRequest request) {
        Instant at = request.at() == null ? Instant.now() : request.at();
        MigrationGraph graph;
        try {
            graph = MigrationGraph.of(request.graph().nodes(), request.graph().edges());
        } catch (IllegalArgumentException e) {
            return error("INVALID_GRAPH", e.getMessage(), request, at);
        }

        List<String> warnings = new java.util.ArrayList<>();
        if (!graph.contains(request.from()) || !graph.contains(request.to())) {
            List<String> missing = new java.util.ArrayList<>();
            if (!graph.contains(request.from())) {
                missing.add("from='" + request.from() + "'");
            }
            if (!graph.contains(request.to())) {
                missing.add("to='" + request.to() + "'");
            }
            return error("UNKNOWN_VERSION",
                    "version id(s) not declared in graph: " + String.join(", ", missing)
                            + " — ids are opaque, nothing is inferred",
                    request, at);
        }
        if (request.from().equals(request.to())) {
            warnings.add("from and to are identical; result is the zero-step path");
        }

        PathPlanner.Plan plan = new PathPlanner().plan(graph, request, at);
        warnings.addAll(plan.warnings());

        String status = plan.paths().isEmpty() ? "NO_PATH" : "FOUND";
        return new PlanResponse(
                status,
                !plan.paths().isEmpty(),
                environment(),
                request.mode().name(),
                request.from(),
                request.to(),
                at,
                plan.paths(),
                plan.blockedEdges(),
                warnings,
                null);
    }

    private PlanResponse error(String code, String message, PlanRequest request, Instant at) {
        return new PlanResponse(
                "ERROR",
                false,
                environment(),
                request.mode() == null ? null : request.mode().name(),
                request.from(),
                request.to(),
                at,
                List.of(),
                List.of(),
                List.of(),
                new ApiError(code, message));
    }

    public EnvironmentInfo environment() {
        return new EnvironmentInfo(
                TzdbInfo.version(),
                System.getProperty("java.version"),
                dataSetName);
    }
}
