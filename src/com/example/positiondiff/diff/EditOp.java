package com.example.positiondiff.diff;

import com.example.positiondiff.model.Line;

/**
 * One atomic, applicable edit. Positions are zero-based line indices.
 *
 * <ul>
 *   <li>EQUAL:   line {@code line} of the old file at {@code oldIndex} is
 *                retained as line {@code newIndex} of the new file.</li>
 *   <li>DELETE:  {@code oldIndex} of the old file is removed; {@code newIndex}
 *                is the running new-file position at that point.</li>
 *   <li>INSERT:  {@code line} becomes {@code newIndex} of the new file;
 *                {@code oldIndex} is the running old-file position at that
 *                point, i.e. the number of old lines already consumed — the
 *                insertion sits between old lines {@code oldIndex-1} and
 *                {@code oldIndex}.</li>
 * </ul>
 *
 * <p>Applying the script only needs {@code oldIndex} on EQUAL/DELETE ops,
 * which visit every old line exactly once in strictly increasing order; the
 * positions on INSERT ops are carried for reporting. Every op carries both
 * absolute positions, so the script is "with position" — no implicit
 * counters are needed to read it.
 */
public final class EditOp {
    public enum Type { EQUAL, DELETE, INSERT }

    private final Type type;
    private final int oldIndex;
    private final int newIndex;
    private final Line line;

    private EditOp(Type type, int oldIndex, int newIndex, Line line) {
        this.type = type;
        this.oldIndex = oldIndex;
        this.newIndex = newIndex;
        if (line == null) {
            throw new IllegalArgumentException("line required for " + type);
        }
        this.line = line;
    }

    public static EditOp equal(Line line, int oldIndex, int newIndex) {
        return new EditOp(Type.EQUAL, oldIndex, newIndex, line);
    }

    public static EditOp delete(Line line, int oldIndex, int newIndex) {
        return new EditOp(Type.DELETE, oldIndex, newIndex, line);
    }

    public static EditOp insert(Line line, int oldIndex, int newIndex) {
        return new EditOp(Type.INSERT, oldIndex, newIndex, line);
    }

    public Type type() { return type; }
    public int oldIndex() { return oldIndex; }
    public int newIndex() { return newIndex; }
    public Line line() { return line; }
}
