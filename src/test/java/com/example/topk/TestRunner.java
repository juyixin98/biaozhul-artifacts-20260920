package com.example.topk;

import com.example.topk.engine.GroupedTopKEngine;
import com.example.topk.engine.GroupResult;
import com.example.topk.engine.QueryResult;
import com.example.topk.engine.RowOrdering;
import com.example.topk.engine.TopKState;
import com.example.topk.json.Json;
import com.example.topk.model.QueryRequest;
import com.example.topk.model.RawRow;
import com.example.topk.model.Row;
import com.example.topk.server.Main;
import com.sun.net.httpserver.HttpServer;

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
import java.util.Random;
import java.util.TreeMap;
import java.util.function.Consumer;

/**
 * Dependency-free automated test suite (plain main + assertions).
 *
 * Acceptance coverage:
 *  1. for many random datasets: every shard count x shard strategy x merge order
 *     x budget matches the full-sort baseline exactly
 *  2. k = 0 is legal and yields empty per-group rows
 *  3. tied values are ordered by stable seq
 *  4. negative k is rejected (engine + HTTP 400)
 *  5. large groups are bounded by the budget yet stay correct; budget < k is rejected
 *  6. state merge is commutative and associative
 *  7. JSON round-trip
 *  8. HTTP endpoint happy path + 400
 *  9. plan/data export files are written
 */
public final class TestRunner {

    private static int passed;
    private static int failed;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) {
        test("full-sort baseline holds across shardings and merge orders", TestRunner::testShardMergeMatrix);
        test("k = 0 yields empty rows for every group", TestRunner::testKZero);
        test("ascending order inverts value ranking, seq still breaks ties", TestRunner::testAscending);
        test("tied values are ordered by stable seq", TestRunner::testTies);
        test("negative k is rejected by the engine", TestRunner::testNegativeK);
        test("explicit duplicate seq is rejected", TestRunner::testDuplicateSeq);
        test("large groups bounded by budget still match full sort; budget < k rejected", TestRunner::testBudget);
        test("state merge is commutative and associative", TestRunner::testMergeAlgebra);
        test("JSON parse/write round-trip", TestRunner::testJsonRoundTrip);
        test("HTTP endpoint: 200 happy path and 400 for negative k", TestRunner::testHttp);
        test("export writes data.json, plan.json, result.json", TestRunner::testExport);
        test("dataFile loading matches inline data", TestRunner::testDataFile);

        System.out.println();
        System.out.println("passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            for (String f : failures) System.out.println("FAIL: " + f);
        }
        System.exit(failed == 0 ? 0 : 1);
    }

    private static void test(String name, ThrowingTest body) {
        try {
            body.run(null);
            passed++;
            System.out.println("PASS  " + name);
        } catch (AssertionError | Exception e) {
            failed++;
            failures.add(name + " -> " + e);
            System.out.println("FAIL  " + name + " -> " + e);
        }
    }

    // ---------------------------------------------------------------- test 1

    static void testShardMergeMatrix(Void v) {
        int[] shardCounts = {1, 2, 3, 5, 8, 16};
        String[] strategies = {"roundRobin", "hash"};
        String[] mergeOrders = {"sequential", "reverse", "pairwise"};
        int[] budgetModes = {0, 1, 2}; // =k, k+3, large
        int configurations = 0;

        for (int seed = 0; seed < 25; seed++) {
            Random rnd = new Random(seed);
            int n = 1 + rnd.nextInt(400);
            List<RawRow> data = randomRows(rnd, n, 1 + rnd.nextInt(8), 1 + rnd.nextInt(30));
            int k = rnd.nextInt(12);
            boolean desc = rnd.nextBoolean();

            Map<String, List<Row>> baseline = fullSortBaseline(data, k, desc);

            for (int sc : shardCounts) {
                for (String strat : strategies) {
                    for (String merge : mergeOrders) {
                        for (int bm : budgetModes) {
                            int budget = switch (bm) {
                                case 0 -> k;
                                case 1 -> k + 3;
                                default -> 100_000;
                            };
                            QueryRequest req = new QueryRequest();
                            req.rows = data;
                            req.k = k;
                            req.order = desc ? "desc" : "asc";
                            req.shards = sc;
                            req.shardStrategy = strat;
                            req.mergeOrder = merge;
                            req.groupBudget = budget;

                            QueryResult result = new GroupedTopKEngine().execute(req);
                            assertResultsEqual(baseline, result,
                                    "seed=" + seed + " shards=" + sc + " strat=" + strat
                                            + " merge=" + merge + " k=" + k + " budget=" + budget);
                            configurations++;
                        }
                    }
                }
            }
        }
        System.out.println("      (verified " + configurations + " engine configurations against full sort)");
        assertTrue(configurations == 25 * 6 * 2 * 3 * 3, "config count sanity");
    }

    // ---------------------------------------------------------------- test 2

    static void testKZero(Void v) {
        List<RawRow> data = List.of(
                new RawRow("a", 1, null), new RawRow("a", 9, null),
                new RawRow("b", 5, null), new RawRow("c", 0, null));
        QueryRequest req = new QueryRequest();
        req.rows = data;
        req.k = 0;
        QueryResult result = new GroupedTopKEngine().execute(req);

        assertEquals(3, result.groups().size(), "all groups still present");
        for (GroupResult g : result.groups()) {
            assertTrue(g.rows().isEmpty(), "group " + g.group() + " must be empty for k=0");
        }
        // and it must hold through every shard/merge configuration
        for (int sc : new int[]{1, 2, 7}) {
            for (String merge : new String[]{"sequential", "reverse", "pairwise"}) {
                req.shards = sc;
                req.mergeOrder = merge;
                req.groupBudget = 0; // extreme budget allowed when k = 0
                QueryResult r2 = new GroupedTopKEngine().execute(req);
                for (GroupResult g : r2.groups()) {
                    assertTrue(g.rows().isEmpty(), "k=0 rows must be empty (" + sc + "/" + merge + ")");
                }
            }
        }
    }

    // ---------------------------------------------------------------- test 3

    static void testAscending(Void v) {
        List<RawRow> data = List.of(
                new RawRow("g", 3, null), new RawRow("g", 1, null), new RawRow("g", 2, null));
        QueryRequest req = new QueryRequest();
        req.rows = data;
        req.k = 2;
        req.order = "asc";
        QueryResult result = new GroupedTopKEngine().execute(req);
        List<Row> rows = result.groups().get(0).rows();
        assertEquals(2, rows.size(), "two rows");
        assertEquals(1L, rows.get(0).value(), "smallest first");
        assertEquals(2L, rows.get(1).value(), "second smallest");
    }

    // ---------------------------------------------------------------- test 4

    static void testTies(Void v) {
        // All values equal: ranking must be strictly the stable seq ascending,
        // independent of how rows are sharded and merged.
        List<RawRow> data = new ArrayList<>();
        for (int i = 0; i < 50; i++) data.add(new RawRow("g", 7, null));
        for (int sc : new int[]{1, 3, 50}) {
            for (String merge : new String[]{"sequential", "reverse", "pairwise"}) {
                QueryRequest req = new QueryRequest();
                req.rows = data;
                req.k = 10;
                req.shards = sc;
                req.mergeOrder = merge;
                QueryResult result = new GroupedTopKEngine().execute(req);
                List<Row> rows = result.groups().get(0).rows();
                assertEquals(10, rows.size(), "10 tied rows kept");
                for (int i = 0; i < 10; i++) {
                    assertEquals((long) i, rows.get(i).seq(),
                            "tie order by seq: shards=" + sc + " merge=" + merge + " i=" + i);
                }
            }
        }
        // explicit seq out of input order must still rank by seq
        List<RawRow> explicit = List.of(
                new RawRow("g", 5, 100L), new RawRow("g", 5, 1L), new RawRow("g", 5, 50L));
        QueryRequest req = new QueryRequest();
        req.rows = explicit;
        req.k = 3;
        QueryResult r2 = new GroupedTopKEngine().execute(req);
        assertEquals(1L, r2.groups().get(0).rows().get(0).seq(), "lowest explicit seq first");
        assertEquals(50L, r2.groups().get(0).rows().get(1).seq(), "then 50");
        assertEquals(100L, r2.groups().get(0).rows().get(2).seq(), "then 100");
    }

    // ---------------------------------------------------------------- test 5

    static void testNegativeK(Void v) {
        QueryRequest req = new QueryRequest();
        req.rows = List.of(new RawRow("a", 1, null));
        req.k = -1;
        assertThrows(IllegalArgumentException.class, () -> new GroupedTopKEngine().execute(req),
                "negative k must be rejected");

        req.k = -100;
        assertThrows(IllegalArgumentException.class, () -> new GroupedTopKEngine().execute(req),
                "large negative k must be rejected");

        // missing k altogether
        QueryRequest noK = new QueryRequest();
        noK.rows = List.of(new RawRow("a", 1, null));
        assertThrows(IllegalArgumentException.class, () -> new GroupedTopKEngine().execute(noK),
                "missing k must be rejected");
    }

    // ---------------------------------------------------------------- test 6

    static void testDuplicateSeq(Void v) {
        QueryRequest req = new QueryRequest();
        req.rows = List.of(new RawRow("a", 1, 5L), new RawRow("a", 2, 5L));
        req.k = 1;
        assertThrows(IllegalArgumentException.class, () -> new GroupedTopKEngine().execute(req),
                "duplicate seq must be rejected");
    }

    // ---------------------------------------------------------------- test 7

    static void testBudget(Void v) {
        Random rnd = new Random(42);
        List<RawRow> big = randomRows(rnd, 5_000, 4, 1_000);
        int k = 5;
        Map<String, List<Row>> baseline = fullSortBaseline(big, k, true);

        for (int budget : new int[]{k, k + 1, 10, 100}) {
            QueryRequest req = new QueryRequest();
            req.rows = big;
            req.k = k;
            req.shards = 6;
            req.groupBudget = budget;
            QueryResult result = new GroupedTopKEngine().execute(req);
            assertResultsEqual(baseline, result, "budget=" + budget);
            // every group exceeded the budget (well over 100 rows/group) and must be marked bounded
            for (GroupResult g : result.groups()) {
                // boundedGroups in plan must contain all groups at budget == k
                if (budget == k) {
                    @SuppressWarnings("unchecked")
                    List<String> bounded = (List<String>) result.plan().get("boundedGroups");
                    assertTrue(bounded.contains(g.group()), "group " + g.group() + " bounded");
                }
            }
        }

        QueryRequest bad = new QueryRequest();
        bad.rows = big;
        bad.k = 10;
        bad.groupBudget = 3; // budget < k cannot hold the required top-k
        assertThrows(IllegalArgumentException.class, () -> new GroupedTopKEngine().execute(bad),
                "budget < k must be rejected");
    }

    // ---------------------------------------------------------------- test 8

    static void testMergeAlgebra(Void v) {
        Random rnd = new Random(7);
        int k = 4, budget = 8;
        RowOrdering ord = new RowOrdering(true);

        List<Row> all = normalize(randomRows(rnd, 60, 3, 10));
        List<Row> p1 = all.subList(0, 20);
        List<Row> p2 = all.subList(20, 40);
        List<Row> p3 = all.subList(40, 60);

        TopKState a = stateOf(p1, k, budget, ord);
        TopKState b = stateOf(p2, k, budget, ord);
        TopKState c = stateOf(p3, k, budget, ord);

        // commutativity: merge(a,b) == merge(b,a)
        assertEquals(a.merged(b).topK(), b.merged(a).topK(), "commutative");
        // associativity: merge(merge(a,b),c) == merge(a,merge(b,c))
        TopKState left = a.merged(b).merged(c);
        TopKState right = a.merged(b.merged(c));
        assertEquals(left.topK(), right.topK(), "associative");
        // identity: merge with empty state is identity
        TopKState empty = new TopKState(k, budget, ord);
        assertEquals(a.merged(empty).topK(), a.topK(), "identity");
        // and both match the plain full-sort top-k of all 60 rows
        List<Row> expected = stateOf(all, k, 1_000_000, ord).topK();
        assertEquals(expected, left.topK(), "algebra result matches full sort");
    }

    // ---------------------------------------------------------------- test 9

    @SuppressWarnings("unchecked")
    static void testJsonRoundTrip(Void v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("k", 3L);
        m.put("names", List.of("a", "b\t\n\"quoted\"", "c"));
        m.put("nested", Map.of("x", 1L, "y", List.of(1.5, -2.0)));
        m.put("nil", null);
        m.put("ok", true);
        String json = Json.write(m);
        Object back = Json.parse(json);
        assertEquals(m, back, "round-trip structure");
        assertEquals(42L, Json.parse("42"), "integer parsing");
        assertThrows(IllegalArgumentException.class, () -> Json.parse("{bad"), "invalid json rejected");
        assertThrows(IllegalArgumentException.class, () -> Json.parse("[1,2,]"), "trailing comma rejected");
    }

    // ---------------------------------------------------------------- test 10

    static void testHttp(Void v) throws Exception {
        HttpServer server = Main.createServer(0);
        server.start();
        int port = server.getAddress().getPort();
        try {
            HttpClient client = HttpClient.newHttpClient();

            // health
            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health")).build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(200, health.statusCode(), "health 200");

            // happy path
            String body = """
                    {"k":2,"shards":3,"mergeOrder":"pairwise","data":[
                      {"group":"a","value":1},{"group":"a","value":4},{"group":"a","value":4},
                      {"group":"b","value":9},{"group":"b","value":2}
                    ]}""";
            HttpResponse<String> ok = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/query"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(200, ok.statusCode(), "query 200: " + ok.body());
            Map<String, Object> parsed = Json.parseObject(ok.body());
            assertEquals(2L, ((Number) parsed.get("groupCount")).longValue(), "two groups");
            // a: top-2 of 4(seq1),4(seq2),1 -> 4 seq1 then 4 seq2 (tie by seq)
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> groups = (List<Map<String, Object>>) parsed.get("groups");
            List<?> aRows = (List<?>) groups.get(0).get("rows");
            assertEquals(2, aRows.size(), "group a keeps 2");
            assertEquals(1L, ((Number) ((Map<?, ?>) aRows.get(0)).get("seq")).longValue(), "tie seq 1");
            assertEquals(2L, ((Number) ((Map<?, ?>) aRows.get(1)).get("seq")).longValue(), "tie seq 2");

            // negative k -> 400
            HttpResponse<String> bad = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/query"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(
                                    "{\"k\":-1,\"data\":[]}")).build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(400, bad.statusCode(), "negative k -> 400");
            assertTrue(bad.body().contains("error"), "error body present");

            // malformed JSON -> 400 (not 500)
            HttpResponse<String> malformed = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/query"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString("{not json")).build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(400, malformed.statusCode(), "malformed json -> 400");
        } finally {
            server.stop(0);
        }
    }

    // ---------------------------------------------------------------- test 11

    static void testExport(Void v) throws Exception {
        Path dir = Files.createTempDirectory("topk-export");
        QueryRequest req = new QueryRequest();
        req.rows = List.of(
                new RawRow("a", 1, null), new RawRow("a", 2, null), new RawRow("b", 5, null));
        req.k = 1;
        req.shards = 2;
        req.exportDir = dir.toString();
        new GroupedTopKEngine().execute(req);

        for (String f : new String[]{"data.json", "plan.json", "result.json"}) {
            Path p = dir.resolve(f);
            assertTrue(Files.exists(p), f + " exported");
            Json.parse(Files.readString(p)); // must be valid JSON
        }
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> dataBack =
                (List<Map<String, Object>>) Json.parse(Files.readString(dir.resolve("data.json")));
        assertEquals(3, dataBack.size(), "exported data row count");
        assertEquals(0L, dataBack.get(0).get("seq"), "exported normalized seq present");
    }

    // ---------------------------------------------------------------- test 12

    static void testDataFile(Void v) throws Exception {
        Path f = Files.createTempFile("data", ".json");
        Files.writeString(f, "[{\"group\":\"g\",\"value\":3},{\"group\":\"g\",\"value\":9}]");

        QueryRequest req = new QueryRequest();
        req.dataFile = f.toString();
        req.k = 1;
        QueryResult result = new GroupedTopKEngine().execute(req);
        assertEquals(9L, result.groups().get(0).rows().get(0).value(), "loaded from file");
    }

    // ---------------------------------------------------------------- helpers

    private static List<RawRow> randomRows(Random rnd, int n, int groupCount, int valueRange) {
        List<RawRow> rows = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            rows.add(new RawRow("g" + rnd.nextInt(groupCount), rnd.nextInt(valueRange), null));
        }
        return rows;
    }

    private static List<Row> normalize(List<RawRow> raw) {
        return GroupedTopKEngine.normalize(raw);
    }

    private static TopKState stateOf(List<Row> rows, int k, int budget, RowOrdering ord) {
        TopKState s = new TopKState(k, budget, ord);
        s.addAll(rows);
        return s;
    }

    /** The reference implementation: group everything, full sort each group, take k. */
    private static Map<String, List<Row>> fullSortBaseline(List<RawRow> raw, int k, boolean desc) {
        RowOrdering ord = new RowOrdering(desc);
        Map<String, List<Row>> byGroup = new TreeMap<>();
        for (Row r : normalize(raw)) byGroup.computeIfAbsent(r.group(), g -> new ArrayList<>()).add(r);
        Map<String, List<Row>> out = new LinkedHashMap<>();
        for (Map.Entry<String, List<Row>> e : byGroup.entrySet()) {
            List<Row> sorted = new ArrayList<>(e.getValue());
            sorted.sort(ord);
            out.put(e.getKey(), List.copyOf(sorted.subList(0, Math.min(k, sorted.size()))));
        }
        return out;
    }

    private static void assertResultsEqual(Map<String, List<Row>> expected, QueryResult actual, String ctx) {
        assertEquals(expected.size(), actual.groups().size(), "group count (" + ctx + ")");
        for (GroupResult g : actual.groups()) {
            List<Row> want = expected.get(g.group());
            assertNotNull(want, "unexpected group " + g.group() + " (" + ctx + ")");
            assertRowsEqual(want, g.rows(), "group " + g.group() + " (" + ctx + ")");
        }
    }

    private static void assertRowsEqual(List<Row> want, List<Row> got, String ctx) {
        assertEquals(want.size(), got.size(), "row count " + ctx);
        for (int i = 0; i < want.size(); i++) {
            Row a = want.get(i);
            Row b = got.get(i);
            if (a.value() != b.value() || a.seq() != b.seq() || !a.group().equals(b.group())) {
                throw new AssertionError(ctx + ": position " + i + " expected " + a + " got " + b);
            }
        }
    }

    private static void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    private static void assertNotNull(Object o, String msg) {
        if (o == null) throw new AssertionError(msg);
    }

    private static void assertEquals(Object expected, Object actual, String msg) {
        if (!expected.equals(actual)) {
            throw new AssertionError(msg + " — expected <" + expected + "> but got <" + actual + ">");
        }
    }

    private static void assertThrows(Class<? extends Throwable> expected, ThrowingRunnable body, String msg) {
        try {
            body.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) return;
            throw new AssertionError(msg + " — expected " + expected + " but got " + t.getClass());
        }
        throw new AssertionError(msg + " — expected " + expected + " but nothing was thrown");
    }

    @FunctionalInterface
    interface ThrowingTest {
        void run(Void v) throws Exception;
    }

    @FunctionalInterface
    interface ThrowingRunnable {
        void run() throws Exception;
    }
}
