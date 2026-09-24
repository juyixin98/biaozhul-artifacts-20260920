package com.example.diff;

import java.util.ArrayList;
import java.util.List;

/**
 * Line-level Myers diff (Myers 1986, "An O(ND) Difference Algorithm") with an
 * explicit budget that can force a degraded result.
 *
 * <p>Budgets:
 * <ul>
 *   <li><b>maxInputChars</b> &mdash; if old+new exceeds this many characters,
 *       Myers is not run at all; the fallback script is returned.</li>
 *   <li><b>maxEditDistance</b> &mdash; Myers only searches diagonals up to this
 *       D (number of line edits). If no path is found by then, the fallback
 *       script is returned.</li>
 * </ul>
 * In both cases {@link DiffResult#degraded} is true and the script is honestly
 * reported as non-shortest. The fallback greedily aligns the longest common
 * line-prefix and line-suffix and replaces the middle &mdash; always valid,
 * often near-optimal, but no claim of shortest is made.
 *
 * <p>Space for the trace is O(D_max * (N+M)) worst case, bounded by the budgets.
 */
public final class MyersDiff {

    /** No distance limit (only the input-size budget applies). */
    public static final int UNLIMITED_DISTANCE = Integer.MAX_VALUE;
    public static final int DEFAULT_MAX_INPUT_CHARS = 2_000_000;

    private final int maxInputChars;
    private final int maxEditDistance;
    private final int context;

    public MyersDiff() {
        this(UNLIMITED_DISTANCE, DEFAULT_MAX_INPUT_CHARS, 3);
    }

    public MyersDiff(int maxEditDistance, int maxInputChars, int context) {
        if (maxEditDistance < 0) {
            throw new IllegalArgumentException("maxEditDistance must be >= 0");
        }
        if (maxInputChars < 0) {
            throw new IllegalArgumentException("maxInputChars must be >= 0");
        }
        this.maxEditDistance = maxEditDistance;
        this.maxInputChars = maxInputChars;
        this.context = context;
    }

    public DiffResult diff(String oldText, String newText) {
        if (oldText == null || newText == null) {
            throw new IllegalArgumentException("inputs must not be null");
        }
        List<Lines.Line> oldL = Lines.split(oldText);
        List<Lines.Line> newL = Lines.split(newText);

        if ((long) oldText.length() + (long) newText.length() > maxInputChars) {
            return fallback(oldL, newL,
                    "input exceeds maxInputChars budget ("
                            + (oldText.length() + newText.length()) + " > " + maxInputChars + ")");
        }

        List<Event> events = myers(oldL, newL, maxEditDistance);
        if (events == null) {
            return fallback(oldL, newL,
                    "edit distance exceeds maxEditDistance budget (" + maxEditDistance + ")");
        }

        List<Edit> edits = toEdits(events, oldL, newL);
        int[] counts = countChanges(edits);
        List<Hunk> hunks = Hunk.build(edits, oldL, newL, context);
        return new DiffResult(false, null, edits, hunks,
                oldL.size(), newL.size(), counts[0], counts[1],
                maxEditDistance, maxInputChars);
    }

    // ------------------------------------------------------------------
    // Myers core
    // ------------------------------------------------------------------

    /** A primitive single-line event in document order: kind + index pair. */
    static final class Event {
        final Edit.Kind kind;
        final int oi; // old index, -1 for INSERT
        final int ni; // new index, -1 for DELETE

        Event(Edit.Kind kind, int oi, int ni) {
            this.kind = kind;
            this.oi = oi;
            this.ni = ni;
        }
    }

    /**
     * Runs Myers. Returns events in reverse document order (as traced back),
     * or null if the distance budget is exhausted before a path is found.
     */
    private static List<Event> myers(List<Lines.Line> a, List<Lines.Line> b, int maxD) {
        int n = a.size();
        int m = b.size();
        if (n == 0 && m == 0) {
            return new ArrayList<>();
        }
        // V[k] = furthest-reaching x after the current D; offset maps k to an index.
        int offset = maxD == Integer.MAX_VALUE ? (n + m) : maxD + 1;
        int width = 2 * offset + 1;
        int[] v = new int[width];
        List<int[]> traces = new ArrayList<>(); // snapshot of V per D (0 is index 1)

        v[offset + 1] = 0;
        for (int d = 0; d <= maxD; d++) {
            int[] snapshot = new int[width];
            traces.add(snapshot);
            for (int k = -d; k <= d; k += 2) {
                int kIdx = k + offset;
                int x;
                if (k == -d || (k != d && v[kIdx - 1] < v[kIdx + 1])) {
                    x = v[kIdx + 1]; // down: insert
                } else {
                    x = v[kIdx - 1] + 1; // right: delete
                }
                int y = x - k;
                // snake: equal lines
                while (x < n && y < m && a.get(x).text.equals(b.get(y).text)) {
                    x++;
                    y++;
                }
                v[kIdx] = x;
                if (x >= n && y >= m) {
                    System.arraycopy(v, 0, snapshot, 0, width);
                    return backtrack(a, b, traces, offset, d);
                }
            }
            System.arraycopy(v, 0, snapshot, 0, width);
        }
        return null;
    }

    private static List<Event> backtrack(List<Lines.Line> a, List<Lines.Line> b,
                                         List<int[]> traces, int offset, int foundD) {
        int n = a.size();
        int m = b.size();
        List<Event> events = new ArrayList<>();
        int x = n;
        int y = m;
        for (int d = foundD; d > 0; d--) {
            int[] v = traces.get(d);
            int k = x - y;
            int prevK;
            boolean down;
            if (k == -d || (k != d && v[k - 1 + offset] < v[k + 1 + offset])) {
                prevK = k + 1;
                down = true; // insert
            } else {
                prevK = k - 1;
                down = false; // delete
            }
            int prevX = v[prevK + offset];
            int prevY = prevX - prevK;
            // snake before the edit
            while (x > prevX && y > prevY) {
                events.add(new Event(Edit.Kind.EQUAL, x - 1, y - 1));
                x--;
                y--;
            }
            if (down) {
                events.add(new Event(Edit.Kind.INSERT, -1, y - 1));
            } else {
                events.add(new Event(Edit.Kind.DELETE, x - 1, -1));
            }
            x = prevX;
            y = prevY;
        }
        // d == 0: remainder must be a snake of equal lines
        while (x > 0 && y > 0) {
            events.add(new Event(Edit.Kind.EQUAL, x - 1, y - 1));
            x--;
            y--;
        }
        return events; // reverse document order
    }

    // ------------------------------------------------------------------
    // Event coalescing
    // ------------------------------------------------------------------

    /**
     * Events come out of backtracking in reverse document order; runs of
     * DELETE and INSERT events naturally alternate there, and each maximal run
     * becomes one coalesced edit once reversed.
     */
    private static List<Edit> toEdits(List<Event> reversed,
                                      List<Lines.Line> oldL, List<Lines.Line> newL) {
        List<Edit> result = new ArrayList<>();
        int n = oldL.size();
        int m = newL.size();
        int[] oldPref = prefixLengths(oldL);
        int[] newPref = prefixLengths(newL);

        // Walk reversed events, accumulate maximal runs (which appear already grouped).
        int i = 0;
        int size = reversed.size();
        while (i < size) {
            Event ev = reversed.get(i);
            int j = i + 1;
            while (j < size && reversed.get(j).kind == ev.kind) {
                j++;
            }
            // events i..j-1 form one run, in reverse document order
            Edit.Kind kind = ev.kind;
            Edit edit;
            int count = j - i;
            switch (kind) {
                case EQUAL: {
                    Event first = reversed.get(j - 1); // smallest indices
                    int os = first.oi;
                    int ns = first.ni;
                    int oe = os + count;
                    int ne = ns + count;
                    List<String> olds = new ArrayList<>();
                    List<String> news = new ArrayList<>();
                    for (int t = 0; t < count; t++) {
                        olds.add(oldL.get(os + t).text);
                        news.add(newL.get(ns + t).text);
                    }
                    edit = new Edit(kind, os, oe, ns, ne,
                            oldPref[os], oldPref[oe], newPref[ns], newPref[ne], olds, news);
                    break;
                }
                case DELETE: {
                    Event first = reversed.get(j - 1);
                    int os = first.oi;
                    int oe = os + count;
                    // new-side line cursor at the document-before boundary
                    int newPos = cursorBefore(reversed, j, false, n, m);
                    List<String> olds = new ArrayList<>();
                    for (int t = os; t < oe; t++) {
                        olds.add(oldL.get(t).text);
                    }
                    edit = new Edit(kind, os, oe, newPos, newPos,
                            oldPref[os], oldPref[oe], newPref[newPos], newPref[newPos],
                            olds, new ArrayList<>());
                    break;
                }
                case INSERT: {
                    Event first = reversed.get(j - 1);
                    int ns = first.ni;
                    int ne = ns + count;
                    int oldPos = cursorBefore(reversed, j, true, n, m);
                    List<String> news = new ArrayList<>();
                    for (int t = ns; t < ne; t++) {
                        news.add(newL.get(t).text);
                    }
                    edit = new Edit(kind, oldPos, oldPos, ns, ne,
                            oldPref[oldPos], oldPref[oldPos], newPref[ns], newPref[ne],
                            new ArrayList<>(), news);
                    break;
                }
                default:
                    throw new IllegalStateException();
            }
            result.add(edit);
            i = j;
        }
        java.util.Collections.reverse(result);
        return result;
    }

    /**
     * Given a run occupying reversed-events indices [0..endExclusive) and the
     * run's own events are excluded, computes the line cursor on the side the
     * run does not touch at the run's document-before boundary.
     *
     * @param oldSide true to compute the old-side cursor for an INSERT run,
     *                false to compute the new-side cursor for a DELETE run
     */
    private static int cursorBefore(List<Event> reversed, int endExclusive, boolean oldSide, int n, int m) {
        // Events at indices >= endExclusive are document-before the run.
        int cursor = 0;
        for (int t = endExclusive; t < reversed.size(); t++) {
            Event e = reversed.get(t);
            if (oldSide) {
                if (e.kind != Edit.Kind.INSERT) {
                    cursor++; // consumes an old line (EQUAL or DELETE)
                }
            } else {
                if (e.kind != Edit.Kind.DELETE) {
                    cursor++; // consumes a new line (EQUAL or INSERT)
                }
            }
        }
        return cursor;
    }

    private static int[] prefixLengths(List<Lines.Line> lines) {
        int[] p = new int[lines.size() + 1];
        for (int i = 0; i < lines.size(); i++) {
            p[i + 1] = p[i] + lines.get(i).text.length();
        }
        return p;
    }

    private static int[] countChanges(List<Edit> edits) {
        int del = 0;
        int ins = 0;
        for (Edit e : edits) {
            if (e.kind == Edit.Kind.DELETE) {
                del += e.oldCount();
            } else if (e.kind == Edit.Kind.INSERT) {
                ins += e.newCount();
            }
        }
        return new int[] {del, ins};
    }

    // ------------------------------------------------------------------
    // Fallback (degraded path)
    // ------------------------------------------------------------------

    /**
     * Greedy valid-but-not-shortest script: align common line prefix and
     * suffix, delete the old middle and insert the new middle.
     */
    private DiffResult fallback(List<Lines.Line> oldL, List<Lines.Line> newL, String reason) {
        List<Edit> edits = new ArrayList<>();
        int n = oldL.size();
        int m = newL.size();
        int[] oldPref = prefixLengths(oldL);
        int[] newPref = prefixLengths(newL);

        int prefix = 0;
        while (prefix < n && prefix < m && oldL.get(prefix).text.equals(newL.get(prefix).text)) {
            prefix++;
        }
        int suffix = 0;
        while (suffix < n - prefix && suffix < m - prefix
                && oldL.get(n - 1 - suffix).text.equals(newL.get(m - 1 - suffix).text)) {
            suffix++;
        }
        int oldMidS = prefix;
        int oldMidE = n - suffix;
        int newMidS = prefix;
        int newMidE = m - suffix;

        if (prefix > 0) {
            edits.add(rangeEdit(Edit.Kind.EQUAL, 0, prefix, 0, prefix, oldPref, newPref, oldL, newL));
        }
        if (oldMidE > oldMidS) {
            edits.add(new Edit(Edit.Kind.DELETE, oldMidS, oldMidE, newMidS, newMidS,
                    oldPref[oldMidS], oldPref[oldMidE],
                    newPref[newMidS], newPref[newMidS],
                    collect(oldL, oldMidS, oldMidE), new ArrayList<>()));
        }
        if (newMidE > newMidS) {
            edits.add(new Edit(Edit.Kind.INSERT, oldMidE, oldMidE, newMidS, newMidE,
                    oldPref[oldMidE], oldPref[oldMidE],
                    newPref[newMidS], newPref[newMidE],
                    new ArrayList<>(), collect(newL, newMidS, newMidE)));
        }
        if (suffix > 0) {
            edits.add(rangeEdit(Edit.Kind.EQUAL, oldMidE, n, newMidE, m, oldPref, newPref, oldL, newL));
        }

        List<Hunk> hunks = Hunk.build(edits, oldL, newL, context);
        int[] counts = countChanges(edits);
        return new DiffResult(true, reason, edits, hunks,
                oldL.size(), newL.size(), counts[0], counts[1],
                maxEditDistance, maxInputChars);
    }

    private static Edit rangeEdit(Edit.Kind kind, int os, int oe, int ns, int ne,
                                  int[] oldPref, int[] newPref,
                                  List<Lines.Line> oldL, List<Lines.Line> newL) {
        return new Edit(kind, os, oe, ns, ne,
                oldPref[os], oldPref[oe], newPref[ns], newPref[ne],
                collect(oldL, os, oe), collect(newL, ns, ne));
    }

    private static List<String> collect(List<Lines.Line> lines, int from, int to) {
        List<String> out = new ArrayList<>();
        for (int i = from; i < to; i++) {
            out.add(lines.get(i).text);
        }
        return out;
    }
}
