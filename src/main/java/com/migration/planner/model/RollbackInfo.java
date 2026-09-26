package com.migration.planner.model;

/**
 * Rollback information at a checkpoint. {@code available} is true only when a real
 * inverse edge exists in the graph; declared reversibility alone never suffices.
 */
public record RollbackInfo(boolean available, String edgeId, Long cost, String reason) {

    public static RollbackInfo available(String edgeId, long cost) {
        return new RollbackInfo(true, edgeId, cost, null);
    }

    public static RollbackInfo unavailable(String reason) {
        return new RollbackInfo(false, null, null, reason);
    }
}
