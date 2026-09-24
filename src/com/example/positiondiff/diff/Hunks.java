package com.example.positiondiff.diff;

import com.example.positiondiff.model.Eol;
import com.example.positiondiff.model.Hunk;
import com.example.positiondiff.model.Line;

import java.util.ArrayList;
import java.util.List;

/**
 * Groups an edit script into unified-diff style hunks with a configurable
 * number of unchanged context lines, and renders them in unified-diff text.
 */
public final class Hunks {

    public static final int DEFAULT_CONTEXT = 3;

    private Hunks() {}

    public static List<Hunk> build(List<EditOp> ops, int oldTotal, int newTotal) {
        return build(ops, oldTotal, newTotal, DEFAULT_CONTEXT);
    }

    public static List<Hunk> build(List<EditOp> ops, int oldTotal, int newTotal, int context) {
        List<Hunk> hunks = new ArrayList<>();
        int n = ops.size();

        // Find change op indices, then greedily merge two change groups when
        // the EQUAL run between them is short enough that their context
        // windows touch (<= 2 * context).
        List<int[]> blocks = new ArrayList<>(); // [lo, hi) op-index ranges of change groups
        int i = 0;
        while (i < n) {
            if (ops.get(i).type() == EditOp.Type.EQUAL) {
                i++;
                continue;
            }
            int start = i;
            while (i < n && ops.get(i).type() != EditOp.Type.EQUAL) i++;
            while (i < n) {
                int equalStart = i;
                while (i < n && ops.get(i).type() == EditOp.Type.EQUAL) i++;
                if (i < n && i - equalStart <= 2 * context) {
                    while (i < n && ops.get(i).type() != EditOp.Type.EQUAL) i++;
                } else {
                    i = equalStart;
                    break;
                }
            }
            blocks.add(new int[]{start, i});
        }

        for (int[] block : blocks) {
            int lo = block[0];
            int hi = block[1];
            int addedBefore = 0;
            while (addedBefore < context && lo - 1 >= 0
                    && ops.get(lo - 1).type() == EditOp.Type.EQUAL) {
                lo--;
                addedBefore++;
            }
            int addedAfter = 0;
            while (addedAfter < context && hi < n
                    && ops.get(hi).type() == EditOp.Type.EQUAL) {
                hi++;
                addedAfter++;
            }

            List<EditOp> slice = new ArrayList<>(ops.subList(lo, hi));
            int oldCount = 0;
            int newCount = 0;
            for (EditOp op : slice) {
                if (op.type() != EditOp.Type.INSERT) oldCount++;
                if (op.type() != EditOp.Type.DELETE) newCount++;
            }

            int oldStart;
            int newStart;
            if (oldCount == 0) {
                // Pure insertion: anchor at the old index of the first insert.
                int idx = slice.get(0).oldIndex();
                oldStart = idx; // zero-based insertion point
            } else {
                oldStart = firstOldIndex(slice);
            }
            if (newCount == 0) {
                int idx = slice.get(0).newIndex();
                newStart = idx;
            } else {
                newStart = firstNewIndex(slice);
            }
            hunks.add(new Hunk(oldStart, oldCount, newStart, newCount, slice));
        }
        return hunks;
    }

    private static int firstOldIndex(List<EditOp> slice) {
        for (EditOp op : slice) {
            if (op.type() != EditOp.Type.INSERT) return op.oldIndex();
        }
        return slice.get(0).oldIndex();
    }

    private static int firstNewIndex(List<EditOp> slice) {
        for (EditOp op : slice) {
            if (op.type() != EditOp.Type.DELETE) return op.newIndex();
        }
        return slice.get(0).newIndex();
    }

    /** Renders a unified diff with {@code --- }/{@code +++ } headers. */
    public static String toUnifiedDiff(String oldName, String newName, List<Hunk> hunks) {
        StringBuilder sb = new StringBuilder();
        sb.append("--- ").append(oldName).append('\n');
        sb.append("+++ ").append(newName).append('\n');
        for (Hunk hunk : hunks) {
            sb.append("@@ -").append(rangeSpec(hunk.oldStart(), hunk.oldCount()))
              .append(" +").append(rangeSpec(hunk.newStart(), hunk.newCount()))
              .append(" @@\n");
            for (EditOp op : hunk.ops()) {
                switch (op.type()) {
                    case EQUAL -> appendLine(sb, ' ', op.line());
                    case DELETE -> appendLine(sb, '-', op.line());
                    case INSERT -> appendLine(sb, '+', op.line());
                }
            }
        }
        return sb.toString();
    }

    private static String rangeSpec(int start, int count) {
        // Convert zero-based half-open to one-based unified-diff range.
        if (count == 0) {
            return (start) + ",0";
        }
        return (start + 1) + (count == 1 ? "" : "," + count);
    }

    private static void appendLine(StringBuilder sb, char prefix, Line line) {
        sb.append(prefix).append(line.text());
        switch (line.eol()) {
            // CRLF is emitted as literal CRLF so the textual diff keeps the
            // exact bytes; per-line eol kinds are always available in JSON.
            case LF -> sb.append('\n');
            case CRLF -> sb.append('\r').append('\n');
            case NONE -> {
                sb.append('\n');
                sb.append("\\ No newline at end of file\n");
            }
        }
    }

    /** Whether an EOL needs any special treatment beyond a plain LF. */
    public static boolean eolMarked(Eol eol) {
        return eol != Eol.LF;
    }
}
