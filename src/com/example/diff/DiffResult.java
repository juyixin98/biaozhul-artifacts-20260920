package com.example.diff;

import java.util.List;

/**
 * Result of a diff run.
 *
 * <p><b>Degradation contract:</b> when a budget limit prevents the exact Myers
 * shortest-edit-script search, {@link #degraded} is {@code true},
 * {@link #shortest} is {@code false} and {@link #degradeReason} explains which
 * budget was hit. The returned edit script is still a valid, APPLICABLE script
 * that transforms old into new &mdash; it is only guaranteed not to be the
 * shortest one. When {@link #degraded} is {@code false}, {@link #shortest} is
 * {@code true} and the script has the minimum possible number of line edits.
 */
public final class DiffResult {
    public final boolean degraded;
    public final boolean shortest;
    public final String degradeReason; // null when not degraded
    public final List<Edit> edits;
    public final List<Hunk> hunks;
    public final int oldLineCount;
    public final int newLineCount;
    public final int deleteCount;
    public final int insertCount;
    /** Distance of the returned script = deleted lines + inserted lines. */
    public final int editDistance;
    /** Budget in effect for this run. */
    public final int maxEditDistance;
    public final int maxInputChars;

    public DiffResult(boolean degraded, String degradeReason,
                      List<Edit> edits, List<Hunk> hunks,
                      int oldLineCount, int newLineCount,
                      int deleteCount, int insertCount,
                      int maxEditDistance, int maxInputChars) {
        this.degraded = degraded;
        this.shortest = !degraded;
        this.degradeReason = degradeReason;
        this.edits = edits;
        this.hunks = hunks;
        this.oldLineCount = oldLineCount;
        this.newLineCount = newLineCount;
        this.deleteCount = deleteCount;
        this.insertCount = insertCount;
        this.editDistance = deleteCount + insertCount;
        this.maxEditDistance = maxEditDistance;
        this.maxInputChars = maxInputChars;
    }
}
