package com.example.positiondiff.model;

import com.example.positiondiff.diff.EditOp;

import java.util.List;

/**
 * A contiguous change region with context. Ranges are zero-based half-open:
 * {@code [oldStart, oldStart+oldCount)} over the old file and likewise for
 * the new file. When a hunk contains zero old (or new) lines — a pure
 * insertion (or deletion) point — {@code oldStart} is the index at which the
 * insertion sits, matching the unified-diff convention.
 */
public final class Hunk {
    private final int oldStart;
    private final int oldCount;
    private final int newStart;
    private final int newCount;
    private final List<EditOp> ops;

    public Hunk(int oldStart, int oldCount, int newStart, int newCount, List<EditOp> ops) {
        this.oldStart = oldStart;
        this.oldCount = oldCount;
        this.newStart = newStart;
        this.newCount = newCount;
        this.ops = ops;
    }

    public int oldStart() { return oldStart; }
    public int oldCount() { return oldCount; }
    public int newStart() { return newStart; }
    public int newCount() { return newCount; }
    public List<EditOp> ops() { return ops; }
}
