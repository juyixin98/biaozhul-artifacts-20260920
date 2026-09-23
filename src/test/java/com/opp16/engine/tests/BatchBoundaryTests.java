package com.opp16.engine.tests;

import com.opp16.engine.Column;
import com.opp16.engine.ColumnarExecutor;
import com.opp16.engine.NullBitmap;
import com.opp16.engine.Predicate;
import com.opp16.engine.QueryPlan;
import com.opp16.engine.RowInterpreter;
import com.opp16.engine.SelectionVector;
import com.opp16.engine.Table;
import com.opp16.engine.json.Json;

import java.util.Arrays;

/**
 * Explicit acceptance for batch boundaries:
 * sizes that straddle 64-row word edges and exact batchSize edges must give
 * identical selection vectors and results as the row interpreter.
 */
public final class BatchBoundaryTests {

    private BatchBoundaryTests() {}

    public static void run() {
        Assert.suite("Batch boundaries");

        for (int n : new int[]{0, 1, 63, 64, 65, 127, 128, 129, 255, 256, 257, 1000}) {
            Table table = buildTable(n);
            String filterJson = """
                    {"op":"and","args":[
                      {"op":"or","args":[
                        {"op":"between","column":"x","low":-1000,"high":10},
                        {"op":"is_null","column":"x"}]},
                      {"op":"not","arg":{"op":"=","column":"s","value":"z"}}
                    ]}
                    """;
            for (int bs : new int[]{1, 2, 7, 31, 63, 64, 65, 100, 128, 1024}) {
                QueryPlan plan = new QueryPlan();
                plan.batchSize = bs;
                plan.predicate = Predicate.fromJson(Json.parse(filterJson));
                plan.predicate.bind(table);

                ColumnarExecutor exec = new ColumnarExecutor(table, plan);
                ColumnarExecutor.SelectionWithStats ss = exec.buildSelection();
                ColumnarExecutor.Result colRes = exec.executeWith(ss.selection);

                RowInterpreter ref = new RowInterpreter(table, plan);
                SelectionVector refSel = ref.buildSelection();
                ColumnarExecutor.Result refRes = ref.execute();

                boolean sel = Arrays.equals(ss.selection.toArray(), refSel.toArray());
                boolean res = com.opp16.engine.QueryEngine.resultsEqual(colRes, refRes);

                // batch accounting: from/to ranges must partition [0,n) with no gaps
                boolean coverage = checkCoverage(ss, n);
                boolean survivorSum = sumSelected(ss) == ss.selection.size();

                Assert.check("n=" + n + " bs=" + bs + " selection matches interpreter", sel);
                Assert.check("n=" + n + " bs=" + bs + " result matches interpreter", res);
                Assert.check("n=" + n + " bs=" + bs + " batches cover [0,n) exactly", coverage);
                Assert.check("n=" + n + " bs=" + bs + " per-batch survivor sums match", survivorSum);
            }
        }

        // Determinism: selection order is ascending for a plain filter scan
        Table t = buildTable(500);
        QueryPlan p = new QueryPlan();
        p.batchSize = 17;
        p.predicate = Predicate.fromJson(Json.parse(
                "{\"op\":\"<\",\"column\":\"x\",\"value\":5}"));
        ColumnarExecutor.SelectionWithStats ss = new ColumnarExecutor(t, p).buildSelection();
        int[] idx = ss.selection.toArray();
        boolean sorted = true;
        for (int i = 1; i < idx.length; i++) if (idx[i] <= idx[i - 1]) sorted = false;
        Assert.check("filter selection vector is strictly ascending", sorted);
    }

    private static Table buildTable(int n) {
        long[] xs = new long[n];
        String[] ss = new String[n];
        NullBitmap xb = new NullBitmap(n);
        NullBitmap sb = new NullBitmap(n);
        for (int i = 0; i < n; i++) {
            // deterministic pattern: every 7th x is null, values cycle around boundaries
            if (i % 7 != 3) { xs[i] = (i % 13) - 3; xb.setPresent(i); }
            if (i % 11 != 5) { ss[i] = (i % 5 == 0) ? "z" : "a" + (i % 4); sb.setPresent(i); }
        }
        Table table = new Table();
        table.add(new Column.IntColumn("x", xs, xb));
        table.add(new Column.StringColumn("s", ss, sb));
        return table;
    }

    private static boolean checkCoverage(ColumnarExecutor.SelectionWithStats ss, int n) {
        var batches = ss.stats.batches();
        if (n == 0) return batches.size() == 1
                && batches.get(0).from == 0 && batches.get(0).to == 0;
        if (batches.isEmpty()) return false;
        if (batches.get(0).from != 0) return false;
        for (int i = 0; i < batches.size(); i++) {
            var b = batches.get(i);
            if (b.from >= b.to && n > 0) return false;
            if (b.to - b.from > ss.stats.getBatchSize()) return false;
            if (i > 0 && b.from != batches.get(i - 1).to) return false;
        }
        return batches.get(batches.size() - 1).to == n;
    }

    private static int sumSelected(ColumnarExecutor.SelectionWithStats ss) {
        int s = 0;
        for (var b : ss.stats.batches()) s += b.selected;
        return s;
    }
}
