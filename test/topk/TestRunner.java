package topk;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.TreeMap;

/**
 * Dependency-free test runner. Each test is a method; failures throw
 * AssertionError and are reported with a non-zero exit code.
 *
 * Acceptance coverage:
 *   - sweeps shard counts and merge orders, comparing against a full-sort
 *     baseline on randomized datasets (including heavy ties);
 *   - k = 0 yields empty results;
 *   - negative k is rejected;
 *   - memory budget forces the bounded-heap strategy for large groups
 *     without changing results;
 *   - k > budget is rejected;
 *   - partial-state merge is associative and commutative.
 */
public final class TestRunner {

    static int passed = 0;
    static int failed = 0;
    static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) {
        run("json round trip", TestRunner::testJsonRoundTrip);
        run("k=0 yields empty groups", TestRunner::testKZero);
        run("negative k rejected", TestRunner::testNegativeK);
        run("k over budget rejected", TestRunner::testKOverBudget);
        run("invalid merge order rejected", TestRunner::testBadMergeOrder);
        run("ties broken by seq", TestRunner::testTies);
        run("budget forces heap, same result", TestRunner::testBudgetStrategies);
        run("merge is associative+commutative", TestRunner::testMergeAlgebra);
        run("sharding/merge-order sweep vs full sort", TestRunner::testSweepVsBaseline);
        run("empty dataset", TestRunner::testEmptyDataset);

        System.out.println();
        System.out.println("passed: " + passed + ", failed: " + failed);
        for (String f : failures) System.out.println("FAILED: " + f);
        System.exit(failed == 0 ? 0 : 1);
    }

    static void run(String name, Runnable test) {
        try {
            test.run();
            passed++;
            System.out.println("PASS " + name);
        } catch (Throwable t) {
            failed++;
            failures.add(name + " -> " + t);
            System.out.println("FAIL " + name + " -> " + t);
        }
    }

    static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    static void checkEq(Object a, Object b, String msg) {
        if (!a.equals(b)) throw new AssertionError(msg + "\n  expected: " + b + "\n  actual:   " + a);
    }

    // ---------- tests ----------

    static void testJsonRoundTrip() {
        Map<String, Object> m = Json.parseObject(
                "{\"k\":3,\"groups\":[\"a\",\"b\"],\"neg\":-7,\"f\":1.5,\"s\":\"x\\n y\",\"b\":true,\"n\":null}");
        checkEq(Json.getLong(m, "k", -1), 3L, "k parse");
        checkEq(Json.getLong(m, "neg", 0), -7L, "negative number");
        checkEq(Json.getString(m, "s"), "x\n y", "escape");
        String again = Json.write(m);
        Map<String, Object> m2 = Json.parseObject(again);
        checkEq(m2, m, "round trip");
    }

    static void testKZero() {
        DataSet ds = sampleData();
        QueryEngine engine = new QueryEngine(ds, 1000);
        QueryEngine.Result r = engine.execute(new TopKQuery(0, 3, null, 1000));
        check(r.groups.size() == 2, "groups present");
        for (List<Row> rows : r.groups.values()) {
            check(rows.isEmpty(), "k=0 must yield empty lists");
        }
        // baseline agrees
        checkEq(r.groups, engine.fullSortBaseline(0), "k=0 vs baseline");
    }

    static void testNegativeK() {
        DataSet ds = sampleData();
        QueryEngine engine = new QueryEngine(ds, 1000);
        expectThrows(() -> engine.execute(new TopKQuery(-1, 1, null, 1000)), "k must be >= 0");
        expectThrows(() -> engine.fullSortBaseline(-5), "k must be >= 0");
        Map<String, Object> req = Json.parseObject("{\"k\":-3}");
        expectThrows(() -> TopKQuery.fromJson(req, 1000), "k must be >= 0");
    }

    static void testKOverBudget() {
        expectThrows(() -> new TopKQuery(11, 1, null, 10), "exceeds per-group memory budget");
    }

    static void testBadMergeOrder() {
        expectThrows(() -> new TopKQuery(1, 3, List.of(0, 0, 1), 10), "permutation");
        expectThrows(() -> new TopKQuery(1, 3, List.of(0, 1), 10), "permutation");
        expectThrows(() -> new TopKQuery(1, 3, List.of(0, 1, 3), 10), "permutation");
    }

    static void testTies() {
        DataSet ds = new DataSet();
        // five identical values in one group: seq order decides, deterministically
        for (int i = 0; i < 5; i++) ds.add("g", 42);
        QueryEngine engine = new QueryEngine(ds, 1000);
        Map<String, List<Row>> r = engine.execute(new TopKQuery(3, 4, List.of(3, 1, 0, 2), 1000)).groups;
        List<Row> top = r.get("g");
        check(top.size() == 3, "top-3 of ties");
        check(top.get(0).seq() == 0 && top.get(1).seq() == 1 && top.get(2).seq() == 2,
                "ties must resolve by ascending seq, got " + top);
    }

    static void testBudgetStrategies() {
        // budget 4: group "big" (9 rows per shard with 1 shard) exceeds it -> heap
        DataSet ds = new DataSet();
        Random rnd = new Random(7);
        for (int i = 0; i < 9; i++) ds.add("big", rnd.nextInt(50));
        ds.add("small", 1);
        ds.add("small", 2);
        QueryEngine engine = new QueryEngine(ds, 4);
        QueryEngine.Result r = engine.execute(new TopKQuery(3, 1, null, 4));
        String plan = r.plan.toJsonString();
        check(plan.contains("bounded-heap"), "big group should use bounded-heap: " + plan);
        check(plan.contains("in-memory-sort"), "small group should use in-memory-sort: " + plan);
        checkEq(r.groups, engine.fullSortBaseline(3), "heap strategy must match full sort");
    }

    static void testMergeAlgebra() {
        DataSet ds = randomData(new Random(99), 200, 6);
        QueryEngine engine = new QueryEngine(ds, 1000);
        // build 5 partials via shards
        List<List<Row>> shards = ds.shard(5);
        List<PartialState> partials = new ArrayList<>();
        for (List<Row> shard : shards) {
            PartialState ps = new PartialState(4);
            for (Row r : shard) ps.add(r);
            partials.add(ps);
        }
        // left fold
        PartialState a = new PartialState(4);
        for (PartialState p : partials) a.mergeFrom(p);
        // right fold
        PartialState b = new PartialState(4);
        for (int i = partials.size() - 1; i >= 0; i--) b.mergeFrom(partials.get(i));
        // shuffled fold
        List<PartialState> shuffled = new ArrayList<>(partials);
        Collections.shuffle(shuffled, new Random(3));
        PartialState c = new PartialState(4);
        for (PartialState p : shuffled) c.mergeFrom(p);
        checkEq(a, b, "left fold == right fold");
        checkEq(a, c, "left fold == shuffled fold");
        checkEq(a.groups(), engine.fullSortBaseline(4), "merged partials == full sort");
    }

    static void testSweepVsBaseline() {
        Random rnd = new Random(20260922);
        for (int trial = 0; trial < 30; trial++) {
            int rows = 1 + rnd.nextInt(300);
            int groups = 1 + rnd.nextInt(8);
            // small value range -> many ties
            DataSet ds = randomData(rnd, rows, groups);
            int budget = 1 + rnd.nextInt(20);
            int k = rnd.nextInt(budget + 1); // k must stay within the per-group budget
            QueryEngine engine = new QueryEngine(ds, budget);
            Map<String, List<Row>> expected = engine.fullSortBaseline(k);
            for (int shards = 1; shards <= 8; shards++) {
                // natural order and one shuffled order per shard count
                checkEq(engine.execute(new TopKQuery(k, shards, null, budget)).groups,
                        expected, "trial " + trial + " shards " + shards);
                List<Integer> perm = new ArrayList<>();
                for (int i = 0; i < shards; i++) perm.add(i);
                Collections.shuffle(perm, rnd);
                checkEq(engine.execute(new TopKQuery(k, shards, perm, budget)).groups,
                        expected, "trial " + trial + " shards " + shards + " perm " + perm);
            }
        }
    }

    static void testEmptyDataset() {
        QueryEngine engine = new QueryEngine(new DataSet(), 10);
        QueryEngine.Result r = engine.execute(new TopKQuery(5, 3, null, 10));
        check(r.groups.isEmpty(), "no groups for empty dataset");
    }

    // ---------- helpers ----------

    static DataSet sampleData() {
        DataSet ds = new DataSet();
        ds.add("a", 10);
        ds.add("a", 20);
        ds.add("b", 5);
        ds.add("b", 7);
        ds.add("b", 7);
        return ds;
    }

    static DataSet randomData(Random rnd, int rows, int groups) {
        DataSet ds = new DataSet();
        for (int i = 0; i < rows; i++) {
            ds.add("g" + rnd.nextInt(groups), rnd.nextInt(20));
        }
        return ds;
    }

    static void expectThrows(Runnable r, String msgPart) {
        try {
            r.run();
        } catch (IllegalArgumentException e) {
            check(e.getMessage() != null && e.getMessage().contains(msgPart),
                    "exception message should contain '" + msgPart + "' but was: " + e.getMessage());
            return;
        }
        throw new AssertionError("expected IllegalArgumentException containing '" + msgPart + "'");
    }
}
