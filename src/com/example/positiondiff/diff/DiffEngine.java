package com.example.positiondiff.diff;

import com.example.positiondiff.model.Hunk;
import com.example.positiondiff.model.Line;

import java.util.ArrayList;
import java.util.List;

/**
 * Facade: turns raw text into lines, runs Myers (with budget), annotates the
 * edit script with absolute positions, groups it into hunks, and applies a
 * script back to text.
 */
public final class DiffEngine {

    private DiffEngine() {}

    public static DiffResult diff(List<Line> oldLines, List<Line> newLines, Budget budget) {
        long started = System.nanoTime();
        Myers.MyersResult r = Myers.diff(oldLines, newLines, budget);
        long elapsedNanos = System.nanoTime() - started;

        int deletes = 0;
        int inserts = 0;

        if (!r.exhausted()) {
            List<EditOp> ops = annotate(r.ops());
            for (EditOp op : ops) {
                if (op.type() == EditOp.Type.DELETE) deletes++;
                else if (op.type() == EditOp.Type.INSERT) inserts++;
            }
            List<Hunk> hunks = Hunks.build(ops, oldLines.size(), newLines.size());
            return DiffResult.optimal(ops, hunks, r.distance(), inserts + deletes,
                    r.nodesUsed(), elapsedNanos);
        }

        // Budget exhausted: produce a guaranteed-valid fallback script by
        // matching an exact prefix and suffix, then deleting/inserting the
        // middle. It is NEVER claimed to be shortest.
        int commonPrefix = commonPrefix(oldLines, newLines);
        int commonSuffix = commonSuffix(oldLines, newLines, commonPrefix);

        List<EditOp> ops = new ArrayList<>();
        for (int i = 0; i < commonPrefix; i++) {
            ops.add(EditOp.equal(oldLines.get(i), i, i));
        }
        for (int i = commonPrefix; i < oldLines.size() - commonSuffix; i++) {
            ops.add(EditOp.delete(oldLines.get(i), i, commonPrefix));
            deletes++;
        }
        for (int j = commonPrefix; j < newLines.size() - commonSuffix; j++) {
            ops.add(EditOp.insert(newLines.get(j), oldLines.size() - commonSuffix, j));
            inserts++;
        }
        for (int i = 0; i < commonSuffix; i++) {
            int oldIdx = oldLines.size() - commonSuffix + i;
            int newIdx = newLines.size() - commonSuffix + i;
            ops.add(EditOp.equal(oldLines.get(oldIdx), oldIdx, newIdx));
        }

        List<Hunk> hunks = Hunks.build(ops, oldLines.size(), newLines.size());
        return DiffResult.degraded(ops, hunks, inserts + deletes,
                r.nodesUsed(), elapsedNanos, r.distance());
    }

    private static List<EditOp> annotate(List<Myers.RawOp> raw) {
        List<EditOp> ops = new ArrayList<>(raw.size());
        int oldIdx = 0;
        int newIdx = 0;
        for (Myers.RawOp op : raw) {
            switch (op.type()) {
                case EQUAL -> {
                    ops.add(EditOp.equal(op.line(), oldIdx, newIdx));
                    oldIdx++;
                    newIdx++;
                }
                case DELETE -> {
                    ops.add(EditOp.delete(op.line(), oldIdx, newIdx));
                    oldIdx++;
                }
                case INSERT -> {
                    ops.add(EditOp.insert(op.line(), oldIdx, newIdx));
                    newIdx++;
                }
            }
        }
        return ops;
    }

    private static int commonPrefix(List<Line> a, List<Line> b) {
        int max = Math.min(a.size(), b.size());
        int i = 0;
        while (i < max && a.get(i).equals(b.get(i))) i++;
        return i;
    }

    private static int commonSuffix(List<Line> a, List<Line> b, int prefixBound) {
        int ai = a.size() - 1;
        int bi = b.size() - 1;
        int count = 0;
        while (ai >= prefixBound && bi >= prefixBound && a.get(ai).equals(b.get(bi))) {
            count++;
            ai--;
            bi--;
        }
        return count;
    }

    /** Applies an edit script to the old line list, returning the new line list. */
    public static List<Line> apply(List<Line> oldLines, List<EditOp> ops) {
        List<Line> result = new ArrayList<>(oldLines.size() + ops.size());
        int cursor = 0;
        for (EditOp op : ops) {
            if (op.oldIndex() != cursor) {
                throw new IllegalArgumentException(
                        "script not sequential at op oldIndex=" + op.oldIndex()
                                + ", expected " + cursor);
            }
            switch (op.type()) {
                case EQUAL -> {
                    if (op.oldIndex() >= oldLines.size()) {
                        throw new IllegalArgumentException("EQUAL past end of old file");
                    }
                    Line expected = oldLines.get(op.oldIndex());
                    if (!expected.equals(op.line())) {
                        throw new IllegalArgumentException(
                                "EQUAL line mismatch at oldIndex=" + op.oldIndex());
                    }
                    result.add(expected);
                    cursor++;
                }
                case DELETE -> {
                    if (op.oldIndex() >= oldLines.size()) {
                        throw new IllegalArgumentException("DELETE past end of old file");
                    }
                    if (!oldLines.get(op.oldIndex()).equals(op.line())) {
                        throw new IllegalArgumentException(
                                "DELETE line mismatch at oldIndex=" + op.oldIndex());
                    }
                    cursor++;
                }
                case INSERT -> result.add(op.line());
            }
        }
        if (cursor != oldLines.size()) {
            throw new IllegalArgumentException(
                    "script does not consume all old lines (at " + cursor
                            + " of " + oldLines.size() + ")");
        }
        return result;
    }
}
