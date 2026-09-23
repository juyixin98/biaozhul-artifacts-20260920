package colscan;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Automated tests, pure JDK:
 *   1. shard statistics (all-NULL, boundary equality)
 *   2. pruning decisions incl. missing statistics (must scan)
 *   3. NULL != 0 semantics at the value/predicate layer
 *   4. columnar file round trip on disk
 *   5. engine: pruned result equals full scan, aggregates + rows
 *   6. byte accounting: pruning reads strictly fewer bytes
 *   7. HTTP end-to-end (ingest, query, /verify)
 */
public final class TestRunner {

    private static int passedGroups;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        group("shard statistics", TestRunner::testShardStats);
        group("pruning decisions", TestRunner::testPruning);
        group("null is never zero", TestRunner::testNullSemantics);
        group("column file round trip", TestRunner::testColumnFile);
        Path tmp = Files.createTempDirectory("colscan-test");
        group("engine pruned vs full scan", () -> testEngine(tmp.resolve("eng")));
        group("http end to end", () -> testHttp(tmp.resolve("http")));

        System.out.println();
        if (failures.isEmpty()) {
            System.out.println("ALL " + passedGroups + " TEST GROUPS PASSED");
        } else {
            System.out.println(failures.size() + " GROUP(S) FAILED:");
            failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }

    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static void group(String name, ThrowingRunnable t) {
        try {
            t.run();
            System.out.println("PASS  " + name);
            passedGroups++;
        } catch (Throwable e) {
            System.out.println("FAIL  " + name + ": " + e);
            failures.add(name + ": " + e.getMessage());
        }
    }

    // ---------- helpers ----------

    static Shard makeShard(String id, long[] values, boolean[] presence) {
        Map<String, long[]> cols = new LinkedHashMap<>();
        cols.put("v", values);
        Map<String, boolean[]> pres = new LinkedHashMap<>();
        pres.put("v", presence);
        return new Shard("t", id, cols, pres);
    }

    static Predicate cmp(String col, String op, long v) {
        return Predicate.parse(Map.of("column", col, "op", op, "value", v));
    }

    // ---------- 1 ----------

    static void testShardStats() {
        Assert a = new Assert();
        boolean[] pres = {true, false, true, false};
        long[] vals = {5, 999_999, 2, -7};
        Shard s = makeShard("x", vals, pres);
        Shard.Stats st = s.stats("v");
        a.eq(st.min, 2L, "min ignores NULL and absent 0");
        a.eq(st.max, 5L, "max over present rows only");
        a.eq(st.nullCount, 2L, "two genuine NULLs");

        boolean[] none = {false, false, false};
        long[] zeros = {0, 0, 0};
        Shard allNull = makeShard("n", zeros, none);
        Shard.Stats ns = allNull.stats("v");
        a.isNull(ns.min, "all-NULL shard has NO min statistic");
        a.isNull(ns.max, "all-NULL shard has NO max statistic");
        a.eq(ns.nullCount, 3L, "nullCount counts all rows");

        Shard stripped = allNull.withMissingStats(List.of("v"));
        Shard.Stats ss = stripped.stats("v");
        a.isNull(ss.min, "missing min");
        a.isNull(ss.max, "missing max");
        a.isNull(ss.nullCount, "missing nullCount forces conservative scan");
        a.check(stripped.presence("v") != null, "data is preserved when stats are stripped");
        System.out.printf("    (%d assertions)%n", a.checkCount());
    }

    // ---------- 2 ----------

    static void testPruning() {
        Assert a = new Assert();
        // [10,10], [10,20], [20,30], all-NULL, missing stats
        Shard s2 = makeShard("s2", new long[]{10, 10, 10},
                new boolean[]{true, true, true});
        Shard s3 = makeShard("s3", new long[]{10, 15, 20},
                new boolean[]{true, true, true});
        Shard s4 = makeShard("s4", new long[]{20, 25, 30},
                new boolean[]{true, true, true});
        Shard s1 = makeShard("s1", new long[]{0, 0, 0},
                new boolean[]{false, false, false});
        Shard s5 = makeShard("s5", new long[]{99, 100},
                new boolean[]{true, true})
                .withMissingStats(List.of("v"));

        // v = 20 : boundary equality — [10,10] pruned, [10,20]/[20,30] kept
        Predicate eq20 = cmp("v", "=", 20);
        a.check(QueryEngine.decide(eq20, 3, s2.stats).prune, "10..10 cannot contain 20");
        a.check(!QueryEngine.decide(eq20, 3, s3.stats).prune, "10..20 might contain 20 (boundary)");
        a.check(!QueryEngine.decide(eq20, 3, s4.stats).prune, "20..30 might contain 20 (boundary)");
        a.check(QueryEngine.decide(eq20, 3, s1.stats).prune,
                "all-NULL shard pruned for numeric equality (NULL != 20)");
        a.check(!QueryEngine.decide(eq20, 2, s5.stats).prune,
                "MISSING stats => must scan even though 99..100 excludes 20");

        // v >= 30 : boundary equality on inclusive comparison
        Predicate ge30 = cmp("v", ">=", 30);
        a.check(QueryEngine.decide(ge30, 3, s2.stats).prune, "max 10 < 30");
        a.check(QueryEngine.decide(ge30, 3, s3.stats).prune, "max 20 < 30");
        a.check(!QueryEngine.decide(ge30, 3, s4.stats).prune, "max 30 >= 30 boundary kept");
        a.check(QueryEngine.decide(ge30, 3, s1.stats).prune, "all-NULL pruned for >=");
        a.check(!QueryEngine.decide(ge30, 2, s5.stats).prune, "missing stats scanned for >=");

        // v > 30 : strictly above boundary, s4 pruned
        Predicate gt30 = cmp("v", ">", 30);
        a.check(QueryEngine.decide(gt30, 3, s4.stats).prune, "max 30 <= 30, nothing > 30");

        // v = 10 with nullCount missing but min/max present: min/max suffice
        Shard.Stats noNullCount = new Shard.Stats(10L, 20L, null);
        Map<String, Shard.Stats> nm = Map.of("v", noNullCount);
        a.check(!QueryEngine.decide(eq20, 3, nm).prune, "min/max present are enough");
        a.check(QueryEngine.decide(cmp("v", "=", 21), 3, nm).prune,
                "21 outside 10..20 pruned without nullCount");

        // v != 10 needs nullCount before pruning [10,10]
        Shard.Stats tenTenNoNulls = new Shard.Stats(10L, 10L, 0L);
        Shard.Stats tenTenUnknownNulls = new Shard.Stats(10L, 10L, null);
        a.check(QueryEngine.decide(cmp("v", "!=", 10), 3, Map.of("v", tenTenNoNulls)).prune,
                "!= 10 pruned only when all rows are 10 AND nullCount=0");
        a.check(!QueryEngine.decide(cmp("v", "!=", 10), 3, Map.of("v", tenTenUnknownNulls)).prune,
                "!= 10 NOT pruned when nulls unknown (NULL != 10 would match)");

        // IS NULL / IS NOT NULL
        Predicate isNull = Predicate.parse(Map.of("column", "v", "op", "IS NULL"));
        Predicate notNull = Predicate.parse(Map.of("column", "v", "op", "IS NOT NULL"));
        a.check(QueryEngine.decide(isNull, 3, s2.stats).prune, "nullCount=0 => IS NULL pruned");
        a.check(QueryEngine.decide(notNull, 3, s1.stats).prune, "all NULL => IS NOT NULL pruned");
        a.check(!QueryEngine.decide(isNull, 3, s5.stats).prune, "missing nullCount => IS NULL scans");
        a.check(!QueryEngine.decide(notNull, 3, s5.stats).prune, "missing nullCount => IS NOT NULL scans");

        // AND / OR
        Predicate and = Predicate.parse(Map.of("op", "AND", "predicates",
                List.of(Map.of("column", "v", "op", ">=", "value", 15),
                        Map.of("column", "v", "op", "<=", "value", 25))));
        a.check(QueryEngine.decide(and, 3, s2.stats).prune, "AND: branch max<15 proves empty");
        a.check(!QueryEngine.decide(and, 3, s3.stats).prune, "AND: [10,20] overlaps 15..25");
        Predicate or = Predicate.parse(Map.of("op", "OR", "predicates",
                List.of(Map.of("column", "v", "op", "<", "value", 5),
                        Map.of("column", "v", "op", ">", "value", 50))));
        a.check(QueryEngine.decide(or, 3, s3.stats).prune, "OR: both branches empty");
        a.check(!QueryEngine.decide(or, 2, s5.stats).prune, "OR: missing stats scans");
        System.out.printf("    (%d assertions)%n", a.checkCount());
    }

    // ---------- 3 ----------

    static void testNullSemantics() {
        Assert a = new Assert();
        String[] ops = {"=", "!=", "<", "<=", ">", ">="};
        for (String op : ops) {
            Predicate p = cmp("v", op, 0);
            final boolean matched = p.test(col -> col.equals("v") ? Value.NULL : Value.of(1));
            a.check(!matched, "NULL " + op + " 0 must be UNKNOWN/false");
        }
        Predicate eqZero = cmp("v", "=", 0);
        a.check(eqZero.test(col -> Value.of(0)), "0 = 0 is true");
        a.check(!eqZero.test(col -> Value.NULL), "NULL = 0 is false (the core trap)");

        Predicate isNull = Predicate.parse(Map.of("column", "v", "op", "IS NULL"));
        a.check(isNull.test(col -> Value.NULL), "IS NULL matches NULL");
        a.check(!isNull.test(col -> Value.of(0)), "IS NULL does not match 0");

        Aggregator.Acc acc = new Aggregator.Acc();
        acc.addRow(Value.NULL);
        acc.addRow(Value.NULL);
        acc.addRow(Value.of(10));
        a.eq(acc.countRows, 3L, "count(*) includes NULL rows");
        a.eq(acc.countNonNull, 1L, "count(v) excludes NULLs");
        a.eq(acc.value(Aggregator.Kind.SUM), 10L, "sum ignores NULLs (does not add zero)");
        a.eq(acc.value(Aggregator.Kind.MIN), 10L, "min ignores NULLs");
        a.eq(acc.value(Aggregator.Kind.MAX), 10L, "max ignores NULLs");
        a.eq(acc.value(Aggregator.Kind.AVG), 10.0, "avg divides by non-null count");
        Aggregator.Acc empty = new Aggregator.Acc();
        empty.addRow(Value.NULL);
        a.isNull(empty.value(Aggregator.Kind.SUM), "sum of only NULLs is null, not 0");
        a.isNull(empty.value(Aggregator.Kind.AVG), "avg of only NULLs is null");
        a.eq(empty.value(Aggregator.Kind.COUNT_ROWS), 1L, "count(*) of one NULL row is 1");
        a.eq(empty.value(Aggregator.Kind.COUNT), 0L, "count(v) of one NULL row is 0");
        System.out.printf("    (%d assertions)%n", a.checkCount());
    }

    // ---------- 4 ----------

    static void testColumnFile() throws IOException {
        Assert a = new Assert();
        Path dir = Files.createTempDirectory("colscan-cf");
        Map<String, long[]> cols = new LinkedHashMap<>();
        cols.put("a", new long[]{1, 2, 3});
        cols.put("b", new long[]{10, 20, 30});
        Map<String, boolean[]> pres = new LinkedHashMap<>();
        pres.put("a", new boolean[]{true, false, true});
        pres.put("b", new boolean[]{false, true, true});
        Shard s = new Shard("tbl", "sh", cols, pres);
        Path f = ColumnFile.write(dir, s);
        ColumnFile.Handle h = ColumnFile.open(f);
        a.eq(h.rowCount, 3, "row count round trip");
        a.eq(h.columns, List.of("a", "b"), "columns round trip");
        a.eq(h.stats.get("a").nullCount, 1L, "nullCount persisted");
        a.eq(h.stats.get("b").min, 20L, "min persisted");

        try (ColumnFile.Scanner sc = ColumnFile.newScanner(h)) {
            ColumnFile.ColumnScanner ca = sc.scanColumn("a");
            a.check(!ca.isNull(0) && ca.getLong(0) == 1, "a[0]=1");
            a.check(ca.isNull(1), "a[1] is NULL on disk");
            a.check(!ca.isNull(2) && ca.getLong(2) == 3, "a[2]=3");
            ColumnFile.ColumnScanner cb = sc.scanColumn("b");
            a.check(cb.isNull(0), "b[0] is NULL on disk");
            a.check(cb.getLong(1) == 20 && cb.getLong(2) == 30, "b values");
        }
        System.out.printf("    (%d assertions)%n", a.checkCount());
    }

    // ---------- 5 + 6 ----------

    static void testEngine(Path dir) throws IOException {
        Assert a = new Assert();
        Catalog cat = new Catalog(dir);
        // s1: all NULL price
        cat.ingest("sales", "s1-all-null", rows(new Long[][]{
                {1L, null, null}, {2L, null, null}, {3L, null, null}}), List.of());
        // s2: price all 10 (boundary)
        cat.ingest("sales", "s2-price-10", rows(new Long[][]{
                {4L, 10L, 1L}, {5L, 10L, 1L}, {6L, 10L, 1L}}), List.of());
        // s3: price 10..20 (boundary)
        cat.ingest("sales", "s3-price-10-20", rows(new Long[][]{
                {7L, 10L, 2L}, {8L, 15L, null}, {9L, 20L, 2L}}), List.of());
        // s4: price 20..30 (boundary)
        cat.ingest("sales", "s4-price-20-30", rows(new Long[][]{
                {10L, 20L, 3L}, {11L, 25L, 3L}, {12L, 30L, 3L}}), List.of());
        // s5: price 99..100 but STATS MISSING for price -> must always be scanned
        cat.ingest("sales", "s5-no-stats", rows(new Long[][]{
                {13L, 99L, 9L}, {14L, 100L, 9L}}), List.of("price"));

        QueryEngine eng = new QueryEngine(cat);
        Predicate priceEq20 = cmp("price", "=", 20);
        List<Aggregator> aggs = Aggregator.parseAll(List.of(
                Map.of("fn", "COUNT_ROWS"),
                Map.of("fn", "SUM", "column", "price", "alias", "sum_price"),
                Map.of("fn", "AVG", "column", "price", "alias", "avg_price"),
                Map.of("fn", "MIN", "column", "price", "alias", "min_price"),
                Map.of("fn", "MAX", "column", "price", "alias", "max_price"),
                Map.of("fn", "SUM", "column", "qty", "alias", "sum_qty")));

        QueryEngine.Result pruned = eng.query("sales", priceEq20, aggs, true,
                QueryEngine.Mode.PRUNED);
        QueryEngine.Result full = eng.query("sales", priceEq20, aggs, true,
                QueryEngine.Mode.FULL_SCAN);

        a.eq(pruned.matchedRowsCount, 2L, "price=20 matches s3 row9 and s4 row10");
        a.eq(pruned.matchedRowsCount, full.matchedRowsCount, "match count equals full scan");
        a.eq(pruned.aggregates, full.aggregates, "aggregates equal full scan");
        a.eq(pruned.rows, full.rows, "matched rows equal full scan");
        a.eq(pruned.aggregates.get("sum_price"), 40L, "20+20=40, no NULL-as-zero");
        a.eq(pruned.aggregates.get("min_price"), 20L, "min=20");
        a.eq(pruned.aggregates.get("max_price"), 20L, "max=20");
        a.eq(pruned.aggregates.get("avg_price"), 20.0, "avg=20");
        a.eq(pruned.aggregates.get("sum_qty"), 5L, "qty 2+3; NULL qty ignored, not zero");

        a.eq(pruned.shardsScanned, 3, "s3, s4 scanned and s5 (missing stats) scanned");
        a.check(pruned.scannedShardIds.contains("s5-no-stats"),
                "missing-stats shard is never pruned");
        a.eq(pruned.prunedShards.size(), 2, "s1 all-NULL and s2 price-10 pruned");
        a.eq(full.shardsScanned, 5, "full scan touches every shard");

        a.check(pruned.bytesRead < full.bytesRead,
                "pruned reads fewer bytes: " + pruned.bytesRead + " < " + full.bytesRead);
        a.check(pruned.bytesRead > 0, "byte accounting is live");
        a.eq(pruned.metadataBytes + pruned.dataBytes, pruned.bytesRead,
                "bytes split into metadata + data");
        a.check(pruned.prunedFileBytes > 0, "whole files avoided are also reported");

        // NULL must not leak as zero into the projection either (row8: price=15, qty NULL)
        QueryEngine.Result nullQty = eng.query("sales", cmp("price", "=", 15),
                List.of(), true, QueryEngine.Mode.PRUNED);
        a.eq(nullQty.rows.size(), 1, "one projected row");
        a.check(nullQty.rows.get(0).get("qty") == null,
                "NULL qty serialized as JSON null, never 0");

        // A second query: price >= 30 — boundary equality keeps s4 (price 30);
        // s5 has no stats so it is scanned too, and its 99/100 legitimately match.
        QueryEngine.Result ge30 = eng.query("sales", cmp("price", ">=", 30),
                Aggregator.parseAll(List.of(Map.of("fn", "SUM", "column", "price"))),
                false, QueryEngine.Mode.PRUNED);
        a.eq(ge30.matchedRowsCount, 3L, "price 30 (s4) + 99,100 (s5, stats missing => scanned)");
        a.check(ge30.scannedShardIds.contains("s5-no-stats"),
                "s5 still scanned for >=30 despite all values 99..100");

        // price IS NULL across all data: only the all-NULL shard matches;
        // s5 cannot be pruned (nullCount missing for price)
        Predicate isNull = Predicate.parse(Map.of("column", "price", "op", "IS NULL"));
        QueryEngine.Result nulls = eng.query("sales", isNull,
                Aggregator.parseAll(List.of(Map.of("fn", "COUNT_ROWS"))),
                false, QueryEngine.Mode.PRUNED);
        a.eq(nulls.matchedRowsCount, 3L, "exactly the three all-NULL rows");
        a.check(!nulls.scannedShardIds.contains("s2-price-10"),
                "nullCount=0 lets IS NULL prune s2");

        // Aggregates over a query that matches a row whose qty is NULL
        QueryEngine.Result mid = eng.query("sales", cmp("price", "=", 15),
                Aggregator.parseAll(List.of(
                        Map.of("fn", "COUNT_ROWS"),
                        Map.of("fn", "COUNT", "column", "qty"),
                        Map.of("fn", "SUM", "column", "qty"),
                        Map.of("fn", "AVG", "column", "qty"))),
                false, QueryEngine.Mode.PRUNED);
        a.eq(mid.aggregates.get("count_rows"), 1L, "1 row matches");
        a.eq(mid.aggregates.get("count_qty"), 0L, "count(qty)=0 for NULL qty");
        a.isNull(mid.aggregates.get("sum_qty"), "sum(qty) over NULL is null");
        a.isNull(mid.aggregates.get("avg_qty"), "avg(qty) over NULL is null");

        // Determinism: identical pruned queries read the same number of bytes.
        QueryEngine.Result again = eng.query("sales", priceEq20, aggs, true,
                QueryEngine.Mode.PRUNED);
        a.eq(again.bytesRead, pruned.bytesRead, "byte accounting deterministic");
        System.out.printf("    (%d assertions, pruned=%d bytes, full=%d bytes, saved=%d)%n",
                a.checkCount(), pruned.bytesRead, full.bytesRead,
                full.bytesRead - pruned.bytesRead);
    }

    private static List<Map<String, Object>> rows(Long[][] triples) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Long[] t : triples) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("id", t[0]);
            r.put("price", t[1]);
            r.put("qty", t[2]);
            rows.add(r);
        }
        return rows;
    }

    // ---------- 7 ----------

    static void testHttp(Path dir) throws Exception {
        Assert a = new Assert();
        Server server = new Server(dir, 0);
        server.start();
        int port = server.getAddressPort();
        try {
            HttpClient client = HttpClient.newHttpClient();
            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            a.eq(health.statusCode(), 200, "health 200");
            a.check(health.body().contains("\"ok\""), "health payload");

            post(port, "/tables/demo/shards/a", Map.of("rows", List.of(
                    Map.of("x", 1), Map.of("x", 2))));
            int status = post(port, "/tables/demo/shards/b", Map.of(
                    "rows", List.of(Map.of("x", 100), Map.of("x", 200)),
                    "missingStats", List.of("x"))).statusCode();
            a.eq(status, 201, "ingest missing-stats shard 201");
            // third shard with honest stats, clearly outside the filter range
            a.eq(post(port, "/tables/demo/shards/c", Map.of("rows", List.of(
                    Map.of("x", 300), Map.of("x", 400)))).statusCode(), 201, "ingest shard c 201");

            // duplicate ingest rejected
            a.eq(post(port, "/tables/demo/shards/a",
                    Map.of("rows", List.of(Map.of("x", 1)))).statusCode(), 400,
                    "duplicate shard rejected");

            HttpResponse<String> v = post(port, "/verify", Map.of(
                    "table", "demo",
                    "filter", Map.of("column", "x", "op", "<", "value", 50),
                    "aggregates", List.of(Map.of("fn", "SUM", "column", "x")),
                    "includeRows", true));
            a.eq(v.statusCode(), 200, "verify returns 200 when results match");
            @SuppressWarnings("unchecked")
            Map<String, Object> vbody = (Map<String, Object>) Json.parse(v.body());
            a.eq(vbody.get("correct"), Boolean.TRUE, "pruned equals full scan over HTTP");
            @SuppressWarnings("unchecked")
            Map<String, Object> pruned = (Map<String, Object>) vbody.get("pruned");
            a.eq(pruned.get("matchedRowsCount"), 2L, "two matching rows");
            a.eq(((List<?>) pruned.get("scannedShardIds")), List.of("a", "b"),
                    "a scanned (matches), b scanned (stats missing), c pruned");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> prunedShards =
                    (List<Map<String, Object>>) pruned.get("prunedShards");
            a.eq(prunedShards.size(), 1, "exactly c pruned");
            a.eq(prunedShards.get(0).get("shardId"), "c", "c is the stats-pruned shard");
            @SuppressWarnings("unchecked")
            Map<String, Object> prunedIo = (Map<String, Object>) pruned.get("io");
            @SuppressWarnings("unchecked")
            Map<String, Object> fullView = (Map<String, Object>) vbody.get("fullScan");
            @SuppressWarnings("unchecked")
            Map<String, Object> fullIo = (Map<String, Object>) fullView.get("io");
            a.check(((Number) prunedIo.get("bytesRead")).longValue()
                            < ((Number) fullIo.get("bytesRead")).longValue(),
                    "HTTP response carries byte savings");

            // bad request shape
            a.eq(post(port, "/query", Map.of("table", "nope",
                    "filter", Map.of("op", "AND", "predicates", List.of()))).statusCode(),
                    400, "unknown table rejected");
        } finally {
            server.stop(0);
        }
        System.out.printf("    (%d assertions)%n", a.checkCount());
    }

    private static HttpResponse<String> post(int port, String path, Object body)
            throws IOException, InterruptedException {
        HttpClient client = HttpClient.newHttpClient();
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(body)))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
