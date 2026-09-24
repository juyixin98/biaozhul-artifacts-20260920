package com.example.positiondiff.diff;

import com.example.positiondiff.model.Hunk;

import java.util.List;

/**
 * Outcome of a diff run.
 *
 * <p>{@code optimal == true} means Myers completed: the script has the minimum
 * number of insert+delete edits (a shortest edit script). When the budget runs
 * out, {@code optimal == false} and {@code degradedReason} is set; the script
 * is still applicable and reconstructs the target exactly, but its length is
 * not claimed minimal.
 */
public final class DiffResult {
    private final List<EditOp> ops;
    private final List<Hunk> hunks;
    private final boolean optimal;
    private final int editDistance;      // inserts + deletes actually in this script
    private final Integer shortestDistance; // proven minimum, only when optimal
    private final long nodesUsed;
    private final long elapsedNanos;
    private final String degradedReason; // null when optimal

    private DiffResult(List<EditOp> ops, List<Hunk> hunks, boolean optimal,
                       int editDistance, Integer shortestDistance,
                       long nodesUsed, long elapsedNanos, String degradedReason) {
        this.ops = ops;
        this.hunks = hunks;
        this.optimal = optimal;
        this.editDistance = editDistance;
        this.shortestDistance = shortestDistance;
        this.nodesUsed = nodesUsed;
        this.elapsedNanos = elapsedNanos;
        this.degradedReason = degradedReason;
    }

    static DiffResult optimal(List<EditOp> ops, List<Hunk> hunks, int shortestDistance,
                              int editDistance, long nodesUsed, long elapsedNanos) {
        return new DiffResult(ops, hunks, true, editDistance, shortestDistance,
                nodesUsed, elapsedNanos, null);
    }

    static DiffResult degraded(List<EditOp> ops, List<Hunk> hunks, int editDistance,
                               long nodesUsed, long elapsedNanos, int reachedD) {
        String reason = "budget exhausted after " + nodesUsed + " search node(s); "
                + "Myers had reached edit-distance layer d=" + reachedD
                + " without proving a shortest script; fallback prefix/suffix script used";
        return new DiffResult(ops, hunks, false, editDistance, null,
                nodesUsed, elapsedNanos, reason);
    }

    public List<EditOp> ops() { return ops; }
    public List<Hunk> hunks() { return hunks; }
    public boolean optimal() { return optimal; }
    public int editDistance() { return editDistance; }
    public Integer shortestDistance() { return shortestDistance; }
    public long nodesUsed() { return nodesUsed; }
    public long elapsedNanos() { return elapsedNanos; }
    public String degradedReason() { return degradedReason; }
}
