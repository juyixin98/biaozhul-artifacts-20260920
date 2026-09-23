package com.opp16.engine.tests;

import com.opp16.engine.Column;
import com.opp16.engine.ColumnType;
import com.opp16.engine.NullBitmap;
import com.opp16.engine.Predicate;
import com.opp16.engine.Table;

import java.util.Random;

public final class PredicateTests {

    private PredicateTests() {}

    public static void run() {
        Assert.suite("Predicate: batch vs row equivalence");

        int n = 300; // spans multiple 64-row words and batches
        Random rnd = new Random(42);
        String[] names = {"alpha", "beta", "gamma", "delta"};
        long[] raw = new long[n];
        String[] strs = new String[n];
        NullBitmap intBm = new NullBitmap(n);
        NullBitmap strBm = new NullBitmap(n);
        for (int i = 0; i < n; i++) {
            int r = rnd.nextInt(10); // 0,1 -> null
            if (r >= 2) {
                raw[i] = rnd.nextInt(20) - 5;
                intBm.setPresent(i);
            }
            if (rnd.nextInt(10) >= 2) {
                strs[i] = names[rnd.nextInt(names.length)];
                strBm.setPresent(i);
            }
        }
        Table table = new Table();
        table.add(new Column.IntColumn("x", raw, intBm));
        table.add(new Column.StringColumn("s", strs, strBm));

        String[] ops = {"=", "!=", "<", "<=", ">", ">="};
        for (String op : ops) {
            Predicate p = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                    "{\"op\":\"" + op + "\",\"column\":\"x\",\"value\":7}"));
            p.bind(table);
            assertBatchEqualsRow("INT " + op + " 7", p, table);
        }
        String[] sops = {"=", "!=", "<", ">="};
        for (String op : sops) {
            Predicate p = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                    "{\"op\":\"" + op + "\",\"column\":\"s\",\"value\":\"gamma\"}"));
            p.bind(table);
            assertBatchEqualsRow("STRING " + op + " 'gamma'", p, table);
        }

        Predicate between = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\"between\",\"column\":\"x\",\"low\":0,\"high\":10}"));
        between.bind(table);
        assertBatchEqualsRow("BETWEEN 0 AND 10", between, table);

        Predicate isNull = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\"is_null\",\"column\":\"x\"}"));
        isNull.bind(table);
        assertBatchEqualsRow("x IS NULL", isNull, table);

        Predicate notNull = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\"is_not_null\",\"column\":\"s\"}"));
        notNull.bind(table);
        assertBatchEqualsRow("s IS NOT NULL", notNull, table);

        Predicate compound = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\"and\",\"args\":["
              + "{\"op\":\"or\",\"args\":["
              + "  {\"op\":\">=\",\"column\":\"x\",\"value\":0},"
              + "  {\"op\":\"is_null\",\"column\":\"x\"}]},"
              + "{\"op\":\"not\",\"arg\":{\"op\":\"=\",\"column\":\"s\",\"value\":\"beta\"}}]}"));
        compound.bind(table);
        assertBatchEqualsRow("(x>=0 OR x IS NULL) AND NOT(s='beta')", compound, table);

        // all-NULL column: every comparison must be UNKNOWN -> zero TRUE rows
        Table allNullTable = new Table();
        allNullTable.add(new Column.IntColumn("x", new long[200], new NullBitmap(200)));
        Predicate cmp = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\">\",\"column\":\"x\",\"value\":0}"));
        cmp.bind(allNullTable);
        assertBatchEqualsRow("all-null INT > 0 (0 survivors)", cmp, allNullTable);
        int trueCount = 0;
        for (int w = 0; w < 200; w += 64) {
            int len = Math.min(64, 200 - w);
            Predicate.TriMask tri = cmp.evalBatch(w, len);
            trueCount += Long.bitCount(tri.trueMask);
            Assert.eq("all-null unknown mask covers every position",
                    Long.bitCount(tri.unknownMask), len);
        }
        Assert.eq("all-null comparison yields zero TRUE bits", trueCount, 0);

        Predicate nullTest = Predicate.fromJson(com.opp16.engine.json.Json.parse(
                "{\"op\":\"is_null\",\"column\":\"x\"}"));
        nullTest.bind(allNullTable);
        assertBatchEqualsRow("all-null IS NULL matches all", nullTest, allNullTable);
    }

    /** Walk every 64-row word and compare TriMask truth bits with evalRow. */
    private static void assertBatchEqualsRow(String name, Predicate p, Table table) {
        int n = table.rowCount();
        boolean ok = true;
        int batchTrue = 0, rowTrue = 0;
        for (int w = 0; w < n; w += 64) {
            int len = Math.min(64, n - w);
            Predicate.TriMask tri = p.evalBatch(w, len);
            long valid = len == 64 ? ~0L : (1L << len) - 1L;
            // no TRUE/UNKNOWN bit may leak past the valid positions
            if (((tri.trueMask | tri.unknownMask) & ~valid) != 0L) ok = false;
            for (int pos = 0; pos < len; pos++) {
                Boolean row = p.evalRow(w + pos);
                boolean batchT = (tri.trueMask & (1L << pos)) != 0;
                boolean batchU = (tri.unknownMask & (1L << pos)) != 0;
                if (batchT) batchTrue++;
                if (Boolean.TRUE.equals(row)) rowTrue++;
                if (Boolean.TRUE.equals(row) != batchT) ok = false;
                if ((row == null) != batchU) ok = false;
                if (batchT && batchU) ok = false; // mutually exclusive
            }
        }
        Assert.check(name + " (true rows: " + rowTrue + ")", ok);
        Assert.eq(name + " true-bit count matches", batchTrue, rowTrue);
    }
}
