package com.example.diff;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Property tests: for many random pairs of SHORT texts, applying the produced
 * edit script to the old text must recover exactly the new text.
 *
 * <p>Generated alphabets deliberately include:
 * <ul>
 *   <li>a tiny alphabet of repeated lines ("a"/"b" lines) &mdash; heavy LCS
 *       ties, the case that breaks naive diffs;</li>
 *   <li>CRLF and LF terminators mixed within a single generation family;</li>
 *   <li>empty lines ("\n") and empty files;</li>
 *   <li>unterminated last lines, including a last line that is just "\r".</li>
 * </ul>
 *
 * <p>For every pair we assert both the EXACT run and the DEGRADED run
 * (forced by maxEditDistance and by maxInputChars) round-trip; the degraded
 * run is also asserted to honestly report degraded=true/shortest=false.
 */
public final class DiffPropertyTests {

    private DiffPropertyTests() {
    }

    public static void register(TestFramework tf) {
        tf.test("random short texts: apply edits recovers target (LF, repeated lines)",
                () -> roundTripTrials(tf, false));
        tf.test("random short texts: apply edits recovers target (CRLF corpus)",
                () -> roundTripTrials(tf, true));
        tf.test("forced distance degradation is applicable and honestly marked",
                DiffPropertyTests::forcedDistanceDegradation);
        tf.test("forced size degradation is applicable and honestly marked",
                DiffPropertyTests::forcedSizeDegradation);
        tf.test("degraded fallback always applicable on random pairs",
                DiffPropertyTests::fallbackAlwaysApplicable);
    }

    private static void roundTripTrials(TestFramework tf, boolean crlf) {
        Random rnd = new Random(crllfSeed(crlf));
        // Small alphabet so repeated/identical lines are extremely common.
        String[] bodies = {"a", "b", "x", "", "same", "y"};
        int trials = 4000;
        for (int t = 0; t < trials; t++) {
            String oldT = randomText(rnd, bodies, crlf);
            String newT = randomText(rnd, bodies, crlf);
            MyersDiff diff = new MyersDiff();
            DiffResult r = diff.diff(oldT, newT);

            // Round trip.
            ApplyEdits.Result applied = ApplyEdits.apply(oldT, r.edits);
            TestFramework.assertTrue(applied.ok,
                    "apply failed: " + applied.error + "\nOLD=<" + oldT + ">\nNEW=<" + newT + ">");
            TestFramework.assertEquals(newT, applied.text,
                    "round-trip mismatch\nOLD=<" + oldT + ">\nNEW=<" + newT + ">\nGOT=<" + applied.text + ">");

            TestFramework.assertFalse(r.degraded, "tiny inputs must never degrade");
            TestFramework.assertTrue(r.shortest, "non-degraded result must claim shortest");

            // The reported distance must equal the actual script cost.
            int actual = r.deleteCount + r.insertCount;
            TestFramework.assertEquals(actual, r.editDistance, "editDistance field mismatch");

            // For these tiny pairs we can verify shortest-ness independently
            // against a classic O(NM) LCS DP reference.
            int lcs = referenceLcsLineCount(oldT, newT);
            int expectedDist = lineCount(oldT) + lineCount(newT) - 2 * lcs;
            TestFramework.assertEquals(expectedDist, r.editDistance,
                    "not shortest for OLD=<" + oldT + "> NEW=<" + newT + ">");
        }
    }

    private static long crllfSeed(boolean crlf) {
        return crlf ? 0xC0FFEEL : 42L;
    }

    private static String randomText(Random rnd, String[] bodies, boolean crlfFamily) {
        // Occasionally produce an empty file.
        if (rnd.nextInt(20) == 0) {
            return "";
        }
        int lines = rnd.nextInt(6); // 0..5 lines
        StringBuilder sb = new StringBuilder();
        boolean crlf = crlfFamily || rnd.nextInt(5) == 0;
        String nl = crlf ? "\r\n" : "\n";
        for (int i = 0; i < lines; i++) {
            String body = bodies[rnd.nextInt(bodies.length)];
            // Most lines are terminated; the last line sometimes isn't.
            boolean last = i == lines - 1;
            boolean terminate = !last || rnd.nextInt(4) != 0;
            sb.append(body);
            if (terminate) {
                sb.append(nl);
            }
        }
        return sb.toString();
    }

    private static void forcedDistanceDegradation() {
        // Inputs whose true line distance is 4: replace all 2 old lines with 2 new.
        String oldT = "p\nq\n";
        String newT = "r\ns\n";
        MyersDiff d = new MyersDiff(2, 1_000_000, 3); // budget below the true distance
        DiffResult r = d.diff(oldT, newT);
        TestFramework.assertTrue(r.degraded, "must degrade when distance exceeds budget");
        TestFramework.assertFalse(r.shortest, "degraded result must not claim shortest");
        TestFramework.assertTrue(r.degradeReason != null && r.degradeReason.contains("maxEditDistance"),
                "reason should name the distance budget");
        ApplyEdits.Result applied = ApplyEdits.apply(oldT, r.edits);
        TestFramework.assertTrue(applied.ok, "degraded script must still apply: " + applied.error);
        TestFramework.assertEquals(newT, applied.text, "degraded script must still recover target");
    }

    private static void forcedSizeDegradation() {
        String oldT = "a\nb\nc\n";
        String newT = "a\nX\nc\n";
        MyersDiff d = new MyersDiff(MyersDiff.UNLIMITED_DISTANCE, 4, 3); // total chars is 12
        DiffResult r = d.diff(oldT, newT);
        TestFramework.assertTrue(r.degraded, "must degrade when size exceeds budget");
        TestFramework.assertFalse(r.shortest, "degraded result must not claim shortest");
        TestFramework.assertTrue(r.degradeReason != null && r.degradeReason.contains("maxInputChars"),
                "reason should name the size budget");
        ApplyEdits.Result applied = ApplyEdits.apply(oldT, r.edits);
        TestFramework.assertTrue(applied.ok, "degraded script must still apply");
        TestFramework.assertEquals(newT, applied.text, "degraded script must recover target");
    }

    private static void fallbackAlwaysApplicable() {
        Random rnd = new Random(777);
        String[] bodies = {"a", "a", "b", "", "c"};
        for (int t = 0; t < 2000; t++) {
            String oldT = randomText(rnd, bodies, rnd.nextBoolean());
            String newT = randomText(rnd, bodies, rnd.nextBoolean());
            // Budget 0: any non-identical pair with edits must degrade immediately.
            MyersDiff d = new MyersDiff(0, 1_000_000, 2);
            DiffResult r = d.diff(oldT, newT);
            if (oldT.equals(newT)) {
                continue;
            }
            TestFramework.assertTrue(r.degraded, "non-identical pair at budget 0 must degrade");
            ApplyEdits.Result applied = ApplyEdits.apply(oldT, r.edits);
            TestFramework.assertTrue(applied.ok, "budget-0 fallback must apply: " + applied.error);
            TestFramework.assertEquals(newT, applied.text, "budget-0 fallback must recover target");
        }
    }

    // ------------------------------------------------------------------
    // Reference implementations
    // ------------------------------------------------------------------

    private static int lineCount(String text) {
        return Lines.split(text).size();
    }

    /** Classic O(NM) LCS over line tokens. */
    private static int referenceLcsLineCount(String oldT, String newT) {
        List<String> a = Lines.texts(Lines.split(oldT));
        List<String> b = Lines.texts(Lines.split(newT));
        int n = a.size();
        int m = b.size();
        int[][] dp = new int[n + 1][m + 1];
        for (int i = n - 1; i >= 0; i--) {
            for (int j = m - 1; j >= 0; j--) {
                if (a.get(i).equals(b.get(j))) {
                    dp[i][j] = dp[i + 1][j + 1] + 1;
                } else {
                    dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
                }
            }
        }
        return dp[0][0];
    }

    /** Kept for other suites that want the DP LCS over arbitrary line lists. */
    public static int referenceLcs(List<String> a, List<String> b) {
        int n = a.size();
        int m = b.size();
        int[][] dp = new int[n + 1][m + 1];
        for (int i = n - 1; i >= 0; i--) {
            for (int j = m - 1; j >= 0; j--) {
                if (a.get(i).equals(b.get(j))) {
                    dp[i][j] = dp[i + 1][j + 1] + 1;
                } else {
                    dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
                }
            }
        }
        return dp[0][0];
    }
}
