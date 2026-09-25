package com.example.editdistance;

/**
 * Levenshtein edit distance computed over Unicode code points.
 *
 * <p>Strings are converted to {@code int[]} of code points before comparison, so
 * supplementary-plane characters (e.g. emoji, which are surrogate pairs in UTF-16)
 * count as ONE unit, and multibyte UTF-8 sequences are never counted per byte.
 * Neither UTF-16 char distance nor byte distance is used anywhere in this project.
 */
public final class Levenshtein {

    private Levenshtein() {
    }

    /** Exact edit distance between two strings, measured in code points. */
    public static int distance(String a, String b) {
        return distance(a.codePoints().toArray(), b.codePoints().toArray());
    }

    /** Exact edit distance between two code-point arrays (full dynamic programming). */
    public static int distance(int[] a, int[] b) {
        int m = a.length;
        int n = b.length;
        if (m == 0) {
            return n;
        }
        if (n == 0) {
            return m;
        }
        int[] prev = new int[n + 1];
        int[] cur = new int[n + 1];
        for (int j = 0; j <= n; j++) {
            prev[j] = j;
        }
        for (int i = 1; i <= m; i++) {
            cur[0] = i;
            for (int j = 1; j <= n; j++) {
                int cost = a[i - 1] == b[j - 1] ? 0 : 1;
                cur[j] = Math.min(
                        Math.min(prev[j] + 1, cur[j - 1] + 1),
                        prev[j - 1] + cost);
            }
            int[] tmp = prev;
            prev = cur;
            cur = tmp;
        }
        return prev[n];
    }

    /**
     * Distance with early exit: returns the exact distance if it is {@code <= max},
     * otherwise returns some value {@code > max} (not necessarily the exact distance).
     *
     * <p>Soundness of the early exit: any alignment path from (0,0) to (m,n) crosses
     * every DP row, and path cost is additive, so the final distance is at least the
     * minimum value of any completed row. If a row minimum already exceeds
     * {@code max}, the true distance cannot be {@code <= max}.
     */
    public static int distanceWithin(int[] a, int[] b, int max) {
        int m = a.length;
        int n = b.length;
        if (Math.abs(m - n) > max) {
            return max + 1;
        }
        if (m == 0) {
            return n;
        }
        if (n == 0) {
            return m;
        }
        int[] prev = new int[n + 1];
        int[] cur = new int[n + 1];
        for (int j = 0; j <= n; j++) {
            prev[j] = j;
        }
        for (int i = 1; i <= m; i++) {
            cur[0] = i;
            int rowMin = cur[0];
            for (int j = 1; j <= n; j++) {
                int cost = a[i - 1] == b[j - 1] ? 0 : 1;
                cur[j] = Math.min(
                        Math.min(prev[j] + 1, cur[j - 1] + 1),
                        prev[j - 1] + cost);
                if (cur[j] < rowMin) {
                    rowMin = cur[j];
                }
            }
            if (rowMin > max) {
                return max + 1;
            }
            int[] tmp = prev;
            prev = cur;
            cur = tmp;
        }
        return prev[n];
    }
}
