package com.example.positiondiff;

import com.example.positiondiff.diff.Budget;
import com.example.positiondiff.diff.DiffEngine;
import com.example.positiondiff.diff.DiffResult;
import com.example.positiondiff.diff.EditOp;
import com.example.positiondiff.diff.Hunks;
import com.example.positiondiff.json.Json;
import com.example.positiondiff.json.JsonException;
import com.example.positiondiff.json.JsonParser;
import com.example.positiondiff.model.Eol;
import com.example.positiondiff.model.Line;
import com.example.positiondiff.search.SearchIndex;
import com.example.positiondiff.search.Tokenizer;
import com.example.positiondiff.text.LineSplitter;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

public final class Tests {

    private static final TestRunner t = new TestRunner();

    public static void main(String[] args) {
        splitterTests();
        handcraftedDiffTests();
        repeatedLinesTests();
        eolAndTrailingNewlineTests();
        shortestScriptTests();
        degradedBudgetTests();
        applyValidationTests();
        hunkTests();
        jsonTests();
        searchTests();
        randomizedRoundTripTests();
        System.exit(t.finish());
    }

    // ------------------------------------------------------------------ lines

    private static void splitterTests() {
        t.group("LineSplitter", () -> {
            t.test("empty string has zero lines", () -> {
                t.assertEquals(0, LineSplitter.split("").size(), "line count");
                t.assertEquals("", LineSplitter.join(LineSplitter.split("")), "join");
            });
            t.test("single newline is one empty LF line", () -> {
                List<Line> l = LineSplitter.split("\n");
                t.assertEquals(1, l.size(), "line count");
                t.assertEquals("", l.get(0).text(), "text");
                t.assertEquals(Eol.LF, l.get(0).eol(), "eol");
            });
            t.test("'a' has NONE eol, 'a\\n' has LF and they differ", () -> {
                List<Line> noNl = LineSplitter.split("a");
                List<Line> withNl = LineSplitter.split("a\n");
                t.assertEquals(Eol.NONE, noNl.get(0).eol(), "NONE");
                t.assertEquals(Eol.LF, withNl.get(0).eol(), "LF");
                t.assertFalse(noNl.equals(withNl), "line lists must differ");
            });
            t.test("CRLF parsed as one terminator", () -> {
                List<Line> l = LineSplitter.split("a\r\nb\r\n");
                t.assertEquals(2, l.size(), "line count");
                t.assertEquals(Eol.CRLF, l.get(0).eol(), "eol0");
                t.assertEquals(Eol.CRLF, l.get(1).eol(), "eol1");
                t.assertEquals("a\r\nb\r\n", LineSplitter.join(l), "roundtrip");
            });
            t.test("mixed LF and CRLF are different lines for same text", () -> {
                List<Line> l = LineSplitter.split("x\nx\r\n");
                t.assertEquals(2, l.size(), "line count");
                t.assertFalse(l.get(0).equals(l.get(1)), "same text, different eol");
            });
            t.test("lone CR is ordinary text (no CR-only line splitting)", () -> {
                List<Line> l = LineSplitter.split("a\rb");
                t.assertEquals(1, l.size(), "line count");
                t.assertEquals("a\rb", l.get(0).text(), "cr kept");
            });
            t.test("last line without newline keeps NONE", () -> {
                List<Line> l = LineSplitter.split("a\nb");
                t.assertEquals(Eol.LF, l.get(0).eol(), "first eol");
                t.assertEquals(Eol.NONE, l.get(1).eol(), "last eol");
                t.assertEquals("a\nb", LineSplitter.join(l), "roundtrip");
            });
            t.test("blank lines: '\\n\\n' gives two empty LF lines", () -> {
                List<Line> l = LineSplitter.split("\n\n");
                t.assertEquals(2, l.size(), "line count");
            });
        });
    }

    // ------------------------------------------------------------- hand diffs

    private static void handcraftedDiffTests() {
        t.group("handcrafted diffs", () -> {
            t.test("empty -> empty: no ops, optimal", () -> {
                DiffResult r = diff("", "");
                t.assertTrue(r.optimal(), "optimal");
                t.assertEquals(0, r.ops().size(), "ops");
            });
            t.test("empty -> 'a\\n': one insert", () -> {
                DiffResult r = diff("", "a\n");
                t.assertTrue(r.optimal(), "optimal");
                t.assertEquals(1, r.editDistance(), "distance");
                assertRoundTrip("", "a\n", r);
            });
            t.test("'a\\n' -> empty: one delete", () -> {
                DiffResult r = diff("a\n", "");
                t.assertEquals(1, r.editDistance(), "distance");
                assertRoundTrip("a\n", "", r);
            });
            t.test("identical multi-line text: zero edits", () -> {
                String s = "a\nb\nc\n";
                DiffResult r = diff(s, s);
                t.assertEquals(0, r.editDistance(), "distance");
                t.assertEquals(3, r.ops().size(), "three equals");
            });
            t.test("middle line changed", () -> {
                DiffResult r = diff("a\nb\nc\n", "a\nB\nc\n");
                t.assertEquals(2, r.editDistance(), "one delete + one insert");
                assertRoundTrip("a\nb\nc\n", "a\nB\nc\n", r);
            });
            t.test("text inserted in the middle", () -> {
                DiffResult r = diff("a\nc\n", "a\nb\nc\n");
                t.assertEquals(1, r.editDistance(), "single insert");
                assertRoundTrip("a\nc\n", "a\nb\nc\n", r);
            });
            t.test("ops carry absolute positions", () -> {
                DiffResult r = diff("a\nb\n", "a\nB\n");
                EditOp delete = r.ops().stream().filter(o -> o.type() == EditOp.Type.DELETE).findFirst().orElseThrow();
                EditOp insert = r.ops().stream().filter(o -> o.type() == EditOp.Type.INSERT).findFirst().orElseThrow();
                t.assertEquals(1, delete.oldIndex(), "delete oldIndex");
                t.assertEquals(1, delete.newIndex(), "delete newIndex");
                // After consuming EQUAL a (old 0) and DELETE b (old 1), the
                // running old position is 2: the insert sits after old line 1.
                t.assertEquals(2, insert.oldIndex(), "insert oldIndex (lines already consumed)");
                t.assertEquals(1, insert.newIndex(), "insert newIndex");
            });
        });
    }

    // -------------------------------------------------------------- repetition

    private static void repeatedLinesTests() {
        t.group("all-repeated lines", () -> {
            t.test("4 identical lines -> 2 identical lines: two deletes, roundtrip", () -> {
                String a = "x\nx\nx\nx\n";
                String b = "x\nx\n";
                DiffResult r = diff(a, b);
                t.assertTrue(r.optimal(), "optimal");
                t.assertEquals(2, r.editDistance(), "distance");
                t.assertEquals(2, count(r, EditOp.Type.EQUAL), "equals");
                assertRoundTrip(a, b, r);
            });
            t.test("2 identical lines -> 4 identical lines: two inserts, roundtrip", () -> {
                String a = "x\nx\n";
                String b = "x\nx\nx\nx\n";
                DiffResult r = diff(a, b);
                t.assertTrue(r.optimal(), "optimal");
                t.assertEquals(2, r.editDistance(), "distance");
                assertRoundTrip(a, b, r);
            });
            t.test("all repeated, identical inputs: zero distance", () -> {
                String s = "same\nsame\nsame\n";
                DiffResult r = diff(s, s);
                t.assertEquals(0, r.editDistance(), "distance");
            });
            t.test("repeated line with one unique line moved in", () -> {
                String a = "s\ns\ns\ns\n";
                String b = "s\ns\nQ\ns\ns\n";
                DiffResult r = diff(a, b);
                t.assertEquals(1, r.editDistance(), "single insert despite repeats");
                assertRoundTrip(a, b, r);
            });
        });
    }

    // ------------------------------------------------------------- EOL content

    private static void eolAndTrailingNewlineTests() {
        t.group("newline form / trailing newline as content", () -> {
            t.test("LF -> CRLF on the same text changes every line", () -> {
                DiffResult r = diff("a\nb\n", "a\r\nb\r\n");
                t.assertEquals(4, r.editDistance(), "both lines replaced");
                assertRoundTrip("a\nb\n", "a\r\nb\r\n", r);
            });
            t.test("adding trailing newline: 'a' -> 'a\\n' is one replacement", () -> {
                DiffResult r = diff("a", "a\n");
                t.assertEquals(2, r.editDistance(), "NONE line replaced by LF line");
                assertRoundTrip("a", "a\n", r);
            });
            t.test("removing trailing newline: 'a\\n' -> 'a'", () -> {
                DiffResult r = diff("a\n", "a");
                t.assertEquals(2, r.editDistance(), "distance");
                assertRoundTrip("a\n", "a", r);
            });
            t.test("CRLF file with last line missing newline", () -> {
                DiffResult r = diff("a\r\nb", "a\r\nB");
                t.assertEquals(2, r.editDistance(), "only last line changed, CRLF first kept");
                assertRoundTrip("a\r\nb", "a\r\nB", r);
            });
            t.test("empty -> CRLF content roundtrip", () -> {
                assertRoundTrip("", "one\r\ntwo\r\n", diff("", "one\r\ntwo\r\n"));
            });
        });
    }

    // ------------------------------------------------------- shortest property

    private static void shortestScriptTests() {
        t.group("optimal result is a shortest edit script", () -> {
            Random rng = new Random(424242);
            for (int iter = 0; iter < 300; iter++) {
                String[] pair = randomTextPair(rng, 9);
                DiffResult r = diff(pair[0], pair[1]);
                t.assertTrue(r.optimal(), "must finish under unlimited budget");
                int expected = lcsDistance(pair[0], pair[1]);
                t.assertEquals(expected, r.editDistance(),
                        "shortest distance for random pair #" + iter);
            }
        });
    }

    // --------------------------------------------------------------- degraded

    private static void degradedBudgetTests() {
        t.group("budget / explicit degradation", () -> {
            t.test("generous budget stays optimal", () -> {
                String[] p = hardPair(12);
                DiffResult r = diffWithBudget(p[0], p[1], new Budget(1_000_000, Budget.UNLIMITED));
                t.assertTrue(r.optimal(), "optimal");
                t.assertEquals(lcsDistance(p[0], p[1]), r.editDistance(), "shortest");
            });

            t.test("zero-node budget degrades and labels the result honestly", () -> {
                String[] p = hardPair(10);
                DiffResult r = diffWithBudget(p[0], p[1], Budget.maxNodes(0));
                t.assertFalse(r.optimal(), "must be marked degraded");
                t.assertTrue(r.degradedReason() != null && r.degradedReason().contains("budget"),
                        "reason must mention the budget");
                t.assertEquals(null, r.shortestDistance(), "no shortest distance claimed");
                // The degraded script MUST still reconstruct the target.
                assertRoundTrip(p[0], p[1], r);
            });

            t.test("tiny-node budget degrades on a hard case but stays valid", () -> {
                String[] p = hardPair(14);
                DiffResult r = diffWithBudget(p[0], p[1], Budget.maxNodes(3));
                t.assertFalse(r.optimal(), "degraded");
                assertRoundTrip(p[0], p[1], r);
                // Honesty guarantee: never flagged shortest.
                t.assertEquals(false, r.optimal(), "optimal flag false");
            });

            t.test("maxD smaller than required degrades but stays valid", () -> {
                String[] p = hardPair(10);
                DiffResult r = diffWithBudget(p[0], p[1], Budget.maxD(1));
                t.assertFalse(r.optimal(), "degraded by d-limit");
                assertRoundTrip(p[0], p[1], r);
            });

            t.test("degraded script is never claimed shortest (50 random hard pairs)", () -> {
                Random rng = new Random(99);
                int degradedSeen = 0;
                for (int i = 0; i < 50; i++) {
                    String[] p = hardPair(rng, 8 + rng.nextInt(6));
                    DiffResult r = diffWithBudget(p[0], p[1], Budget.maxNodes(1 + rng.nextInt(5)));
                    if (!r.optimal()) {
                        degradedSeen++;
                        assertRoundTrip(p[0], p[1], r);
                    }
                }
                t.assertTrue(degradedSeen > 0, "budget must actually trigger degradation");
            });

            t.test("identical files stay optimal even with zero budget", () -> {
                DiffResult r = diffWithBudget("a\nb\n", "a\nb\n", Budget.maxNodes(0));
                t.assertTrue(r.optimal(), "found immediately at d=0");
            });
        });
    }

    // ------------------------------------------------------- apply validation

    private static void applyValidationTests() {
        t.group("apply rejects malformed scripts", () -> {
            List<Line> oldLines = LineSplitter.split("a\nb\n");
            t.test("non-sequential oldIndex rejected", () -> {
                List<EditOp> ops = List.of(
                        EditOp.equal(new Line("a", Eol.LF), 0, 0),
                        EditOp.equal(new Line("b", Eol.LF), 2, 1));
                expectIae(() -> DiffEngine.apply(oldLines, ops));
            });
            t.test("EQUAL line mismatch rejected", () -> {
                List<EditOp> ops = List.of(
                        EditOp.equal(new Line("X", Eol.LF), 0, 0),
                        EditOp.equal(new Line("b", Eol.LF), 1, 1));
                expectIae(() -> DiffEngine.apply(oldLines, ops));
            });
            t.test("script not consuming all lines rejected", () -> {
                List<EditOp> ops = List.of(EditOp.equal(new Line("a", Eol.LF), 0, 0));
                expectIae(() -> DiffEngine.apply(oldLines, ops));
            });
        });
    }

    // ------------------------------------------------------------------ hunks

    private static void hunkTests() {
        t.group("hunk ranges", () -> {
            t.test("single change produces one hunk with covering ranges", () -> {
                DiffResult r = diff("a\nb\nc\nX\nd\ne\nf\ng\nh\n", "a\nb\nc\nY\nd\ne\nf\ng\nh\n");
                t.assertEquals(1, r.hunks().size(), "one hunk");
                var h = r.hunks().get(0);
                // Context 3 around the single changed line -> 7 lines each side.
                t.assertEquals(7, h.oldCount(), "old count incl. context");
                t.assertEquals(7, h.newCount(), "new count incl. context");
                t.assertEquals(0, h.oldStart(), "hunk starts at first context line");
                int changed = 0;
                for (var op : h.ops()) if (op.type() != EditOp.Type.EQUAL) changed++;
                t.assertEquals(2, changed, "one delete + one insert inside hunk");
            });
            t.test("distant changes produce separate hunks", () -> {
                DiffResult r = diff("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n",
                                    "A\nb\nc\nd\ne\nf\ng\nh\ni\nJ\n");
                t.assertEquals(2, r.hunks().size(), "two hunks");
                assertRoundTrip("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n",
                        "A\nb\nc\nd\ne\nf\ng\nh\ni\nJ\n", r);
            });
        });
    }

    // ------------------------------------------------------------------- json

    private static void jsonTests() {
        t.group("JSON parser", () -> {
            t.test("roundtrips CRLF and unicode escapes", () -> {
                String input = "{\"a\":\"line1\\r\\nline2\",\"b\":[1,2.5,true,false,null],\"中文\":\"值\"}";
                Object parsed = JsonParser.parse(input);
                String again = Json.stringify(parsed);
                Object reparsed = JsonParser.parse(again);
                t.assertEquals(parsed, reparsed, "roundtrip");
            });
            t.test("rejects malformed JSON", () -> {
                boolean thrown = false;
                try { JsonParser.parse("{\"a\":}"); } catch (JsonException e) { thrown = true; }
                t.assertTrue(thrown, "JsonException expected");
            });
            t.test("rejects trailing characters", () -> {
                boolean thrown = false;
                try { JsonParser.parse("{}garbage"); } catch (JsonException e) { thrown = true; }
                t.assertTrue(thrown, "JsonException expected");
            });
        });
    }

    // ---------------------------------------------------------------- search

    private static void searchTests() {
        t.group("local search over synthetic corpus", () -> {
            SearchIndex index = SearchIndex.synthetic();

            t.test("english term ranks docs containing it", () -> {
                var resp = index.search("myers budget", 10);
                t.assertTrue(resp.hits().size() >= 2, "multiple docs match");
                t.assertTrue(resp.hits().get(0).score() >= resp.hits().get(1).score(),
                        "scores sorted desc");
            });
            t.test("CJK unigram and bigram match", () -> {
                var unigram = index.search("差", 10);
                t.assertTrue(!unigram.hits().isEmpty(), "unigram matches");
                var bigram = index.search("差异", 10);
                t.assertTrue(!bigram.hits().isEmpty(), "bigram matches");
            });
            t.test("query with no matches returns empty hits", () -> {
                var resp = index.search("zzzznotpresent", 10);
                t.assertEquals(0, resp.hits().size(), "no hits");
            });
            t.test("hit reports 1-based line number", () -> {
                var resp = index.search("bootstrap", 10);
                t.assertEquals(1, resp.hits().size(), "one doc");
                t.assertEquals(1, resp.hits().get(0).lineNumber(), "first line of NOTES.log");
            });
            t.test("tokenizer splits latin words and CJK runs", () -> {
                var toks = Tokenizer.tokenize("Myers 差异算法 v2");
                t.assertTrue(toks.contains("myers"), "latin lowercased");
                t.assertTrue(toks.contains("差"), "cjk unigram");
                t.assertTrue(toks.contains("差异"), "cjk bigram");
                t.assertTrue(toks.contains("v2"), "alnum token");
            });
        });
    }

    // ------------------------------------------------------ randomized property

    private static void randomizedRoundTripTests() {
        t.group("randomized apply round-trip (2000 cases)", () -> {
            Random rng = new Random(20260924L);
            int degraded = 0;
            for (int iter = 0; iter < 2000; iter++) {
                String[] pair = randomTextPair(rng, 8);
                Budget budget;
                // Occasionally constrain the budget to exercise degraded paths too.
                if (iter % 37 == 0) budget = Budget.maxNodes(rng.nextInt(6));
                else budget = Budget.UNLIMITED_BUDGET;

                DiffResult r = diffWithBudget(pair[0], pair[1], budget);
                if (!r.optimal()) {
                    degraded++;
                    // The contract for degraded results: applicable and exact,
                    // but shortestDistance must be absent.
                    t.assertEquals(null, r.shortestDistance(), "no shortest claim when degraded");
                } else {
                    t.assertEquals(lcsDistance(pair[0], pair[1]), r.editDistance(),
                            "optimal must be shortest (#" + iter + ")");
                }
                assertRoundTrip(pair[0], pair[1], r);
            }
            t.assertTrue(degraded > 0, "some iterations must exercise degradation");
            System.out.println("    (degraded cases in this run: " + degraded + ")");
        });
    }

    // ---------------------------------------------------------------- helpers

    private static DiffResult diff(String a, String b) {
        return diffWithBudget(a, b, Budget.UNLIMITED_BUDGET);
    }

    private static DiffResult diffWithBudget(String a, String b, Budget budget) {
        return DiffEngine.diff(LineSplitter.split(a), LineSplitter.split(b), budget);
    }

    private static void assertRoundTrip(String oldText, String expectedNew, DiffResult r) {
        List<Line> got = DiffEngine.apply(LineSplitter.split(oldText), r.ops());
        String joined = LineSplitter.join(got);
        t.assertEquals(expectedNew, joined, "apply must reconstruct target exactly");
    }

    private static int count(DiffResult r, EditOp.Type type) {
        int c = 0;
        for (EditOp op : r.ops()) if (op.type() == type) c++;
        return c;
    }

    private static void expectIae(Runnable r) {
        try {
            r.run();
        } catch (IllegalArgumentException expected) {
            return;
        }
        throw new AssertionError("expected IllegalArgumentException");
    }

    /**
     * Generates a random short document pair. Line TEXT alphabet is tiny so
     * matches and repeats are frequent; each line independently gets LF,
     * CRLF, or (for the last line only) no terminator, so trailing-newline
     * and mixed-ending cases are exercised.
     */
    private static String[] randomTextPair(Random rng, int maxLines) {
        return new String[]{ randomText(rng, maxLines), randomText(rng, maxLines) };
    }

    private static String randomText(Random rng, int maxLines) {
        int lineCount = rng.nextInt(maxLines + 1);
        if (lineCount == 0) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < lineCount; i++) {
            int wordRuns = rng.nextInt(3);            // 0..2 repeated tokens
            String token = switch (rng.nextInt(4)) {
                case 0 -> "a";
                case 1 -> "b";
                case 2 -> "x";
                default -> "行";
            };
            sb.append(token.repeat(wordRuns));
            if (i < lineCount - 1) {
                sb.append(rng.nextBoolean() ? "\n" : "\r\n");
            } else {
                switch (rng.nextInt(3)) {
                    case 0 -> sb.append("\n");
                    case 1 -> sb.append("\r\n");
                    default -> { /* no trailing newline */ }
                }
            }
        }
        return sb.toString();
    }

    /** A genuinely Myers-hard pair: ascending vs descending permutation. */
    private static String[] hardPair(int n) {
        return new String[]{ hardText(n, false), hardText(n, true) };
    }

    private static String[] hardPair(Random rng, int n) {
        List<Integer> order = new ArrayList<>();
        for (int i = 0; i < n; i++) order.add(i);
        java.util.Collections.shuffle(order, rng);
        StringBuilder asc = new StringBuilder();
        StringBuilder shuffled = new StringBuilder();
        for (int i = 0; i < n; i++) {
            asc.append("L").append(i).append('\n');
            shuffled.append("L").append(order.get(i)).append('\n');
        }
        return new String[]{ asc.toString(), shuffled.toString() };
    }

    private static String hardText(int n, boolean descending) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) {
            int v = descending ? n - 1 - i : i;
            sb.append("L").append(v).append('\n');
        }
        return sb.toString();
    }

    /**
     * Independent reference edit distance via O(NM) LCS dynamic programming
     * over the SAME line objects, so EOL and trailing newline count.
     */
    private static int lcsDistance(String a, String b) {
        List<Line> x = LineSplitter.split(a);
        List<Line> y = LineSplitter.split(b);
        int n = x.size();
        int m = y.size();
        int[][] dp = new int[n + 1][m + 1];
        for (int i = n - 1; i >= 0; i--) {
            for (int j = m - 1; j >= 0; j--) {
                if (x.get(i).equals(y.get(j))) dp[i][j] = dp[i + 1][j + 1] + 1;
                else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
        return n + m - 2 * dp[0][0];
    }
}
