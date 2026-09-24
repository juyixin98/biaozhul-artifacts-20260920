package com.example.diff;

/** One rendered line inside a hunk, unified-diff style. */
public final class HunkLine {
    public enum Kind { CONTEXT, DELETED, ADDED }

    public final Kind kind;
    /** Full line text including its terminator (as tokenized by {@link Lines}). */
    public final String text;
    public final int oldLine; // 1-based line number in old text, 0 if not present
    public final int newLine; // 1-based line number in new text, 0 if not present

    public HunkLine(Kind kind, String text, int oldLine, int newLine) {
        this.kind = kind;
        this.text = text;
        this.oldLine = oldLine;
        this.newLine = newLine;
    }
}
