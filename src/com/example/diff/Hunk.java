package com.example.diff;

import java.util.ArrayList;
import java.util.List;

/**
 * A unified-diff style hunk with a configurable context window.
 *
 * <p>Header coordinates follow {@code @@ -oldStart,oldCount +newStart,newCount @@}
 * with 1-based line numbers; a count of 1 is rendered without the comma, and a
 * count of 0 renders at the boundary after the last line (unified diff
 * convention, e.g. {@code -0,0} for insertion into an empty file).
 */
public final class Hunk {
    public final int oldStart; // 1-based, or equal to oldCount boundary when count == 0
    public final int oldCount;
    public final int newStart; // 1-based
    public final int newCount;
    public final List<HunkLine> lines;

    public Hunk(int oldStart, int oldCount, int newStart, int newCount, List<HunkLine> lines) {
        this.oldStart = oldStart;
        this.oldCount = oldCount;
        this.newStart = newStart;
        this.newCount = newCount;
        this.lines = lines;
    }

    public String header() {
        return "@@ -" + range(oldStart, oldCount) + " +" + range(newStart, newCount) + " @@";
    }

    private static String range(int start, int count) {
        if (count == 1) {
            return Integer.toString(start);
        }
        return start + "," + count;
    }

    /**
     * Groups edits into hunks, taking {@code context} unchanged lines on each
     * side and merging windows that overlap or touch.
     */
    public static List<Hunk> build(List<Edit> edits, List<Lines.Line> oldL, List<Lines.Line> newL, int context) {
        List<Edit> changes = new ArrayList<>();
        for (Edit e : edits) {
            if (e.kind != Edit.Kind.EQUAL) {
                changes.add(e);
            }
        }
        if (changes.isEmpty()) {
            return new ArrayList<>();
        }

        // Context windows over both coordinate systems, half-open line ranges.
        List<int[]> windows = new ArrayList<>();
        for (Edit e : changes) {
            int[] w = new int[] {
                    Math.max(0, e.oldStart - context),
                    Math.min(oldL.size(), e.oldEnd + context),
                    Math.max(0, e.newStart - context),
                    Math.min(newL.size(), e.newEnd + context)
            };
            if (windows.isEmpty()) {
                windows.add(w);
            } else {
                int[] last = windows.get(windows.size() - 1);
                if (w[0] <= last[1] && w[2] <= last[3]) {
                    last[1] = Math.max(last[1], w[1]);
                    last[3] = Math.max(last[3], w[3]);
                } else {
                    windows.add(w);
                }
            }
        }

        List<Hunk> hunks = new ArrayList<>();
        for (int[] w : windows) {
            int oldLo = w[0], oldHi = w[1], newLo = w[2], newHi = w[3];
            List<HunkLine> hl = new ArrayList<>();
            for (Edit e : edits) {
                switch (e.kind) {
                    case EQUAL: {
                        int from = Math.max(e.oldStart, oldLo);
                        int to = Math.min(e.oldEnd, oldHi);
                        int shift = from - e.oldStart;
                        for (int k = 0; k < to - from; k++) {
                            String t = oldL.get(from + k).text;
                            hl.add(new HunkLine(HunkLine.Kind.CONTEXT, t, from + k + 1,
                                    e.newStart + shift + k + 1));
                        }
                        break;
                    }
                    case DELETE: {
                        int from = Math.max(e.oldStart, oldLo);
                        int to = Math.min(e.oldEnd, oldHi);
                        for (int k = from; k < to; k++) {
                            hl.add(new HunkLine(HunkLine.Kind.DELETED, oldL.get(k).text, k + 1, 0));
                        }
                        break;
                    }
                    case INSERT: {
                        int from = Math.max(e.newStart, newLo);
                        int to = Math.min(e.newEnd, newHi);
                        for (int k = from; k < to; k++) {
                            hl.add(new HunkLine(HunkLine.Kind.ADDED, newL.get(k).text, 0, k + 1));
                        }
                        break;
                    }
                }
            }
            int hOldStart = oldHi == oldLo ? oldLo : oldLo + 1;
            int hNewStart = newHi == newLo ? newLo : newLo + 1;
            hunks.add(new Hunk(hOldStart, oldHi - oldLo, hNewStart, newHi - newLo, hl));
        }
        return hunks;
    }
}
