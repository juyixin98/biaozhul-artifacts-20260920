package com.migration.planner.model;

import java.util.List;

/**
 * A successful migration plan: ordered steps, checkpoints after each step,
 * equal-cost alternatives (if any), and environment metadata including the
 * timezone database version of the running JVM.
 */
public record MigrationPlan(
        boolean ok,
        String from,
        String to,
        long totalCost,
        List<PlanStep> steps,
        List<Checkpoint> checkpoints,
        List<AlternativePath> alternatives,
        boolean tieBrokenDeterministically,
        boolean alternativesTruncated,
        List<String> warnings,
        String graphId,
        String graphVersion,
        String tzdbVersion) {
}
