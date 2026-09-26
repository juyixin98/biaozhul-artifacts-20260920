package com.example.migration.model;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;
import java.util.List;

/**
 * Serialized planning result. {@code status} is FOUND / NO_PATH / ERROR.
 * Never inferred from version numbers; every step cites a real declared edge.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record PlanResponse(
        String status,
        boolean success,
        EnvironmentInfo environment,
        String mode,
        String from,
        String to,
        Instant evaluatedAt,
        List<PathResult> paths,
        List<BlockedEdge> blockedEdges,
        List<String> warnings,
        ApiError error
) {
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record EnvironmentInfo(String tzdbVersion, String javaVersion, String dataSet) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record PathResult(
            int rank,
            double totalCost,
            int stepCount,
            Checkpoint initialCheckpoint,
            List<StepResult> steps,
            Checkpoint finalCheckpoint
    ) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record StepResult(
            int sequence,
            String action,
            String from,
            String to,
            double cost,
            String description,
            TimeWindow timeWindow,
            boolean reverseEdgeExists,
            Boolean reverseEdgeMarkedReversible,
            Checkpoint checkpoint
    ) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record TimeWindow(Instant validFrom, Instant validTo) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Checkpoint(String id, String version, double cumulativeCost, String note) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record BlockedEdge(String from, String to, List<String> reasons) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ApiError(String code, String message) {
    }
}
