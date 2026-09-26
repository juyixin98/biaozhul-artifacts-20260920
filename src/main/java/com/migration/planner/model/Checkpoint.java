package com.migration.planner.model;

/** State checkpoint reached after a given step of the plan. */
public record Checkpoint(int afterStep, String atVersion, long cumulativeCost, RollbackInfo rollback) {
}
