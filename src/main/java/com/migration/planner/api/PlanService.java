package com.migration.planner.api;

import com.migration.planner.model.MigrationPlan;
import com.migration.planner.model.PlanRequest;
import com.migration.planner.model.SchemaGraph;
import com.migration.planner.plan.PathPlanner;
import com.migration.planner.plan.PlanBuilder;
import com.migration.planner.plan.PlanException;
import com.migration.planner.tz.TzdbInfo;

/** Orchestrates request validation, path search, and plan assembly. */
public final class PlanService {

    private static final int MAX_ALTERNATIVES_LIMIT = 64;

    private final SchemaGraph graph;
    private final PathPlanner planner;
    private final PlanBuilder builder;

    public PlanService(SchemaGraph graph) {
        this.graph = graph;
        this.planner = new PathPlanner();
        this.builder = new PlanBuilder();
    }

    public MigrationPlan plan(PlanRequest request) {
        validate(request);
        int maxPaths = request.maxAlternatives() == null
                ? PathPlanner.DEFAULT_MAX_PATHS
                : Math.min(request.maxAlternatives() + 1, MAX_ALTERNATIVES_LIMIT);
        PathPlanner.SearchResult result = planner.findBestPaths(
                graph, request.from(), request.to(), request.contextOrEmpty(), maxPaths);
        return builder.build(graph, result, request.from(), request.to(), TzdbInfo.currentTzdbVersion());
    }

    private void validate(PlanRequest request) {
        if (request == null) {
            throw new PlanException(PlanException.Code.INVALID_REQUEST, "request body must be a JSON object");
        }
        if (request.from() == null || request.from().isBlank()) {
            throw new PlanException(PlanException.Code.INVALID_REQUEST, "'from' is required");
        }
        if (request.to() == null || request.to().isBlank()) {
            throw new PlanException(PlanException.Code.INVALID_REQUEST, "'to' is required");
        }
        if (request.maxAlternatives() != null && request.maxAlternatives() < 1) {
            throw new PlanException(PlanException.Code.INVALID_REQUEST,
                    "'maxAlternatives' must be >= 1");
        }
    }

    public SchemaGraph graph() {
        return graph;
    }
}
