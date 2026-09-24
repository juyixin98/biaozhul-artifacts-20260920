package com.example.diff;

import java.util.List;

/**
 * One applied edit in a line-level edit script, ordered from document start
 * to end.
 *
 * <p>Positions are 0-based half-open ranges over LINES and over CHARACTERS of
 * the original inputs:
 * <ul>
 *   <li>EQUAL:  oldLines=[oldStart,oldEnd), newLines=[newStart,newEnd), text carried too</li>
 *   <li>DELETE: oldLines=[oldStart,oldEnd) are removed; newStart==newEnd</li>
 *   <li>INSERT: newLines=[newStart,newEnd) are inserted at oldStart==oldEnd</li>
 * </ul>
 * Consecutive primitive edits of the same kind are coalesced, so e.g. a block
 * of 3 deleted lines is one DELETE edit.
 */
public final class Edit {
    public enum Kind { EQUAL, DELETE, INSERT }

    public final Kind kind;
    public final int oldStart; // line index, inclusive
    public final int oldEnd;   // line index, exclusive
    public final int newStart;
    public final int newEnd;
    /** Character offsets into the old text. */
    public final int oldCharStart;
    public final int oldCharEnd;
    /** Character offsets into the new text. */
    public final int newCharStart;
    public final int newCharEnd;
    public final List<String> oldLines;
    public final List<String> newLines;

    public Edit(Kind kind,
                int oldStart, int oldEnd, int newStart, int newEnd,
                int oldCharStart, int oldCharEnd,
                int newCharStart, int newCharEnd,
                List<String> oldLines, List<String> newLines) {
        this.kind = kind;
        this.oldStart = oldStart;
        this.oldEnd = oldEnd;
        this.newStart = newStart;
        this.newEnd = newEnd;
        this.oldCharStart = oldCharStart;
        this.oldCharEnd = oldCharEnd;
        this.newCharStart = newCharStart;
        this.newCharEnd = newCharEnd;
        this.oldLines = oldLines;
        this.newLines = newLines;
    }

    public int oldCount() {
        return oldEnd - oldStart;
    }

    public int newCount() {
        return newEnd - newStart;
    }
}
