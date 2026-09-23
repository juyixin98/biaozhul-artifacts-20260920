package bitmapindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.TreeSet;

/**
 * Acceptance tests for the multi-dimensional bitmap index:
 *   1. random boolean expressions vs brute-force row scan (thousands of cases)
 *   2. empty universes (fresh index, zero-row load, all rows deleted)
 *   3. high-cardinality column (one distinct value per row)
 *   4. NOT after deletions never leaks deleted ids; complements match scan
 *   5. row ids are stable across deletions (no silent compaction)
 *   6. index space statistics
 */
public final class BitMapIndexAcceptanceTest {

    static final String[] CITIES = {"Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu", "Xi'an"};
    static final String[] COLORS = {"red", "green", "blue", "amber", "violet"};
    static final String[] CATEGORIES = new String[24];

    static {
        for (int i = 0; i < CATEGORIES.length; i++) {
            CATEGORIES[i] = "cat-" + i;
        }
    }

    static final class Dataset {
        List<Map<String, Object>> rows;
        List<String> columns;
        Map<String, List<String>> distinctValues = new LinkedHashMap<>();
    }

    /** Build a synthetic dataset with low/medium/high cardinality columns. */
    static Dataset generate(Random rnd, int n, boolean uniqueSku) {
        Dataset ds = new Dataset();
        ds.columns = List.of("city", "color", "category", "active", "sku");
        for (String c : ds.columns) {
            ds.distinctValues.put(c, new ArrayList<>());
        }
        ds.rows = new ArrayList<>(n);
        TreeSet<String> skus = new TreeSet<>();
        for (int i = 0; i < n; i++) {
            Map<String, Object> row = new LinkedHashMap<>();
            String city = CITIES[rnd.nextInt(CITIES.length)];
            String color = COLORS[rnd.nextInt(COLORS.length)];
            String cat = CATEGORIES[rnd.nextInt(CATEGORIES.length)];
            String active = rnd.nextInt(100) < 80 ? "yes" : "no";
            String sku = uniqueSku
                    ? String.format("SKU-%08d", i)
                    : String.format("SKU-%05d", rnd.nextInt(Math.max(2, n / 3)));
            row.put("city", city);
            row.put("color", color);
            row.put("category", cat);
            row.put("active", active);
            row.put("sku", sku);
            ds.rows.add(row);
            addDistinct(ds, "city", city);
            addDistinct(ds, "color", color);
            addDistinct(ds, "category", cat);
            addDistinct(ds, "active", active);
            addDistinct(ds, "sku", sku);
        }
        return ds;
    }

    private static void addDistinct(Dataset ds, String col, String v) {
        List<String> vs = ds.distinctValues.get(col);
        if (!vs.contains(v)) {
            vs.add(v);
        }
    }

    /** Random nested boolean expression; leaf = eq predicate. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> randomExpr(Random rnd, Dataset ds, int depth) {
        if (depth <= 0 || rnd.nextInt(100) < 45) {
            String col = ds.columns.get(rnd.nextInt(ds.columns.size()));
            String value;
            List<String> values = ds.distinctValues.get(col);
            if (rnd.nextInt(100) < 80) {
                value = values.get(rnd.nextInt(values.size()));
            } else {
                // value that does not exist -> empty bitmap, exercising NOT edge
                value = "__nonexistent_" + rnd.nextInt(1_000_000);
            }
            Map<String, Object> leaf = new LinkedHashMap<>();
            leaf.put("op", "eq");
            leaf.put("column", col);
            leaf.put("value", value);
            return leaf;
        }
        int kind = rnd.nextInt(3);
        Map<String, Object> node = new LinkedHashMap<>();
        if (kind == 0) {
            node.put("op", "not");
            node.put("arg", randomExpr(rnd, ds, depth - 1));
        } else {
            node.put("op", kind == 1 ? "and" : "or");
            int argc = 2 + rnd.nextInt(2); // 2 or 3
            List<Object> args = new ArrayList<>(argc);
            for (int i = 0; i < argc; i++) {
                args.add(randomExpr(rnd, ds, depth - 1));
            }
            node.put("args", args);
        }
        return node;
    }

    public static void run(Asserts a) {
        randomVsScan(a);
        emptyUniverses(a);
        highCardinality(a);
        notAfterDeletion(a);
        stableRowIds(a);
        spaceStatistics(a);
        invalidRequests(a);
    }

    /* ------------------------------------------------------------------ */

    private static void randomVsScan(Asserts a) {
        a.caseName("accept/random-expr-vs-scan");
        Random rnd = new Random(20260923);
        Dataset ds = generate(rnd, 5000, false);
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);

        int cases = 3000;
        for (int t = 0; t < cases; t++) {
            Object expr = randomExpr(rnd, ds, 4);
            List<Integer> scan = index.scan(expr);
            int[] bitmap = index.query(expr).toArray();
            if (!exactMatch(scan, bitmap)) {
                a.check(false, "expr " + t + " mismatch scan=" + scan.size()
                        + " bitmap=" + bitmap.length + " expr=" + Json.write(expr).trim());
                return;
            }
        }
        a.check(true, cases + " random expressions match row scan (5,000 rows, no deletions)");

        // repeat with deletions interleaved
        int deleted = 0;
        for (int t = 0; t < cases; t++) {
            if (t % 137 == 0) {
                int victim = rnd.nextInt(5000);
                if (index.isLive(victim)) {
                    index.delete(victim);
                    deleted++;
                }
            }
            Object expr = randomExpr(rnd, ds, 4);
            List<Integer> scan = index.scan(expr);
            int[] bitmap = index.query(expr).toArray();
            if (!exactMatch(scan, bitmap)) {
                a.check(false, "post-delete expr " + t + " mismatch (deleted so far=" + deleted + ")");
                return;
            }
        }
        a.check(true, cases + " random expressions match scan with " + deleted + " interleaved deletions");
    }

    private static void emptyUniverses(Asserts a) {
        a.caseName("accept/empty-universe");
        Random rnd = new Random(7);

        // (1) fresh index, nothing loaded: unknown columns are a client error,
        // but counts are zero and NOT cannot manufacture rows.
        BitMapIndex fresh = new BitMapIndex();
        a.eqInt(fresh.liveCount(), 0, "fresh live count 0");
        a.eqInt(fresh.totalRows(), 0, "fresh total rows 0");

        // (2) dataset with zero rows loaded (columns registered, universe empty)
        Dataset empty = generate(rnd, 0, false);
        BitMapIndex idx2 = new BitMapIndex();
        idx2.load(empty.rows, empty.columns);
        a.eqInt(idx2.totalRows(), 0, "zero rows loaded");
        a.eqInt(idx2.query(leaf("city", "Beijing")).cardinality(), 0, "eq on empty universe is empty");
        a.eqInt(idx2.query(not(leaf("city", "Beijing"))).cardinality(), 0,
                "NOT on empty universe is empty (not the whole 32-bit space)");
        a.eqInt(idx2.query(randomExpr(rnd, empty, 3)).cardinality(), 0,
                "random expr on zero-row dataset is empty");

        // (3) delete every row, then run random expressions including NOT
        Dataset ds = generate(rnd, 2000, false);
        BitMapIndex idx3 = new BitMapIndex();
        idx3.load(ds.rows, ds.columns);
        for (int i = 0; i < 2000; i++) {
            idx3.delete(i);
        }
        a.eqInt(idx3.liveCount(), 0, "all rows deleted -> live count 0");
        for (int t = 0; t < 500; t++) {
            Object e = randomExpr(rnd, ds, 4);
            a.eqInt(idx3.query(e).cardinality(), 0, "expr yields nothing on all-deleted universe t=" + t);
            if (t % 50 == 0) {
                // restore one row mid-loop to prove mask is the live source, then delete again
                idx3.restore(123);
                a.eqInt(idx3.query(not(leaf("city", "__none__"))).cardinality(), 1,
                        "NOT after partial restore sees exactly the live rows");
                idx3.delete(123);
            }
        }
    }

    private static void highCardinality(Asserts a) {
        a.caseName("accept/high-cardinality");
        Random rnd = new Random(99);
        int n = 20000;
        Dataset ds = generate(rnd, n, true); // sku unique per row
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);
        Map<String, Object> stats = index.stats();
        a.check(stats.containsKey("indexBytes") && stats.containsKey("perColumn")
                && stats.containsKey("compressionRatioVsUncompressed"), "stats map has expected keys");

        List<String> skus = ds.distinctValues.get("sku");
        a.eqInt(skus.size(), n, n + " distinct skus (unique column)");

        for (int t = 0; t < 500; t++) {
            String sku = skus.get(rnd.nextInt(skus.size()));
            RoaringBitmap hits = index.query(leaf("sku", sku));
            List<Integer> scan = index.scan(leaf("sku", sku));
            a.eqInt(hits.cardinality(), 1, "unique sku matches exactly one row t=" + t);
            if (!exactMatch(scan, hits.toArray())) {
                a.check(false, "unique sku scan mismatch t=" + t);
                return;
            }
        }
        a.check(true, "500 random unique-value lookups each match exactly one row (20,000 rows)");

        // nonexistent unique value
        a.eqInt(index.query(leaf("sku", "SKU-DOES-NOT-EXIST")).cardinality(), 0,
                "nonexistent high-card value -> empty");
        // NOT of it must be all live rows, not anything larger
        a.eqInt(index.query(not(leaf("sku", "SKU-DOES-NOT-EXIST"))).cardinality(), n,
                "NOT(nonexistent) over full universe = all live rows");
    }

    private static void notAfterDeletion(Asserts a) {
        a.caseName("accept/not-after-deletion");
        Random rnd = new Random(31337);
        int n = 8000;
        Dataset ds = generate(rnd, n, false);
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);

        // delete a structured chunk + random rows
        boolean[] deleted = new boolean[n];
        for (int i = 100; i < 300; i++) {
            deleted[i] = true;
            index.delete(i);
        }
        for (int k = 0; k < 1500; k++) {
            int id = rnd.nextInt(n);
            if (!deleted[id]) {
                deleted[id] = true;
                index.delete(id);
            }
        }
        int expectedLive = n - countTrue(deleted);
        a.eqInt(index.liveCount(), expectedLive, "live count after structured+random deletes");

        for (int t = 0; t < 1000; t++) {
            Object expr = randomExpr(rnd, ds, 4);
            int[] bitmap = index.query(expr).toArray();
            List<Integer> scan = index.scan(expr);
            if (!exactMatch(scan, bitmap)) {
                a.check(false, "NOT-era mismatch t=" + t);
                return;
            }
            for (int id : bitmap) {
                if (deleted[id]) {
                    a.check(false, "deleted id " + id + " leaked into query result t=" + t);
                    return;
                }
            }
        }
        a.check(true, "1,000 random expressions after deletion match scan and contain no deleted id");

        // direct targeted check: NOT(eq city=Beijing) must be live rows whose city != Beijing
        Object notBeijing = not(leaf("city", "Beijing"));
        int[] hits = index.query(notBeijing).toArray();
        for (int id : hits) {
            Map<String, String> row = index.getRow(id);
            if ("Beijing".equals(row.get("city")) || deleted[id]) {
                a.check(false, "NOT city=Beijing returned a Beijing or deleted row id=" + id);
                return;
            }
        }
        a.check(true, "NOT(city=Beijing) result verified row-by-row against original data");

        // count of NOT result + count of predicate result == live count (partition of live set)
        int yes = index.query(leaf("city", "Beijing")).cardinality();
        int no = index.query(notBeijing).cardinality();
        a.eqInt(yes + no, expectedLive, "predicate and its NOT partition the LIVE universe");
    }

    private static void stableRowIds(Asserts a) {
        a.caseName("accept/stable-row-ids");
        Random rnd = new Random(555);
        int n = 3000;
        Dataset ds = generate(rnd, n, false);
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);

        // snapshot ids of some value before deletion
        String city = "Shenzhen";
        int[] before = index.query(leaf("city", city)).toArray();
        TreeSet<Integer> beforeSet = new TreeSet<>();
        for (int id : before) {
            beforeSet.add(id);
        }

        // delete a swath of low ids — compaction would shift every later id
        for (int i = 0; i < 1000; i++) {
            index.delete(i);
        }
        int[] after = index.query(leaf("city", city)).toArray();
        for (int id : after) {
            if (!beforeSet.contains(id)) {
                a.check(false, "query returned id " + id + " that never matched (renumbering!)");
                return;
            }
            // the record behind the id must be the SAME original record
            Map<String, Object> original = ds.rows.get(id);
            Map<String, String> fetched = index.getRow(id);
            for (String c : ds.columns) {
                if (!String.valueOf(original.get(c)).equals(fetched.get(c))) {
                    a.check(false, "row " + id + " content changed after deletions column=" + c);
                    return;
                }
            }
        }
        int surviving = 0;
        for (int id : before) {
            if (id >= 1000) {
                surviving++;
            }
        }
        a.eqInt(after.length, surviving, "exactly the pre-deletion survivors keep their same ids");
        a.check(true, "row ids and row contents stable after deleting first 1,000 rows");
    }

    private static void spaceStatistics(Asserts a) {
        a.caseName("accept/space-statistics");
        Random rnd = new Random(8080);
        Dataset ds = generate(rnd, 10000, false);
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);

        Map<String, Object> stats = index.stats();
        a.eqInt(((Number) stats.get("totalRows")).longValue(), 10000, "stats totalRows");
        a.eqInt(((Number) stats.get("liveRows")).longValue(), 10000, "stats liveRows");
        long indexBytes = ((Number) stats.get("indexBytes")).longValue();
        long rawBytes = ((Number) stats.get("uncompressedBitmapBytes")).longValue();
        a.check(indexBytes > 0, "indexBytes positive");
        a.check(rawBytes > indexBytes, "compressed index smaller than naive bit-slices ("
                + indexBytes + " vs " + rawBytes + " bytes)");
        double ratio = ((Number) stats.get("compressionRatioVsUncompressed")).doubleValue();
        a.check(ratio > 0 && ratio < 1.0, "compression ratio in (0,1): " + ratio);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> perCol = (List<Map<String, Object>>) stats.get("perColumn");
        Map<String, Object> skuStat = perCol.stream()
                .filter(m -> "sku".equals(m.get("column"))).findFirst().orElseThrow();
        a.eqInt(((Number) skuStat.get("distinctValues")).longValue(),
                ds.distinctValues.get("sku").size(), "sku distinct count in stats");
        // sparse unique values: everything should be array containers
        a.eqInt(((Number) skuStat.get("bitmapContainers")).longValue(), 0,
                "high-cardinality one-bit bitmaps are array containers");

        Map<String, Object> activeStat = perCol.stream()
                .filter(m -> "active".equals(m.get("column"))).findFirst().orElseThrow();
        a.eqInt(((Number) activeStat.get("bitmapContainers")).longValue(), 1,
                "dense active=yes (~80% of 10k rows) uses a bitmap container");
        Map<String, Object> cityStat = perCol.stream()
                .filter(m -> "city".equals(m.get("column"))).findFirst().orElseThrow();
        a.eqInt(((Number) cityStat.get("bitmapContainers")).longValue(), 0,
                "city value bitmaps (~1.7k/value in a 10k chunk) stay array containers");
        a.check(true, "space statistics complete: indexBytes=" + indexBytes
                + " uncompressed=" + rawBytes + " ratio=" + ratio);
    }

    private static void invalidRequests(Asserts a) {
        a.caseName("accept/invalid-expr");
        Random rnd = new Random(1);
        Dataset ds = generate(rnd, 100, false);
        BitMapIndex index = new BitMapIndex();
        index.load(ds.rows, ds.columns);
        expectError(a, index, Map.of("op", "xor"), "unknown op rejected");
        expectError(a, index, Map.of("op", "eq", "column", "nope", "value", "x"),
                "unknown column rejected");
        expectError(a, index, Map.of("op", "and", "args", List.of()), "empty AND is allowed identity");
        // empty AND returns live universe by design (identity element)
        a.eqInt(index.query(Map.of("op", "and", "args", List.of())).cardinality(), 100,
                "AND with no predicates = live universe (identity)");
        a.eqInt(index.query(Map.of("op", "or", "args", List.of())).cardinality(), 0,
                "OR with no predicates = empty (identity)");
        try {
            index.delete(100);
            a.check(false, "out-of-range delete must throw");
        } catch (IllegalArgumentException ok) {
            a.check(true, "out-of-range delete rejected: " + ok.getMessage());
        }
    }

    private static void expectError(Asserts a, BitMapIndex index, Map<String, Object> expr, String msg) {
        try {
            index.query(expr);
            // empty AND is valid; special case
            if ("and".equals(expr.get("op"))) {
                a.check(true, msg);
                return;
            }
            a.check(false, msg + " (no error thrown)");
        } catch (RuntimeException e) {
            a.check(true, msg + ": " + e.getMessage());
        }
    }

    /* ------------------------------------------------------------------ */
    /* helpers                                                             */
    /* ------------------------------------------------------------------ */

    static Map<String, Object> leaf(String col, String value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("op", "eq");
        m.put("column", col);
        m.put("value", value);
        return m;
    }

    static Map<String, Object> not(Map<String, Object> arg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("op", "not");
        m.put("arg", arg);
        return m;
    }

    static boolean exactMatch(List<Integer> expected, int[] actual) {
        if (expected.size() != actual.length) {
            return false;
        }
        for (int i = 0; i < actual.length; i++) {
            if (expected.get(i) != actual[i]) {
                return false;
            }
        }
        return true;
    }

    private static int countTrue(boolean[] xs) {
        int c = 0;
        for (boolean x : xs) {
            if (x) {
                c++;
            }
        }
        return c;
    }
}
