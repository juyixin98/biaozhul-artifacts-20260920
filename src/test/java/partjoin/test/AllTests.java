package partjoin.test;

import partjoin.HashJoinEngine;
import partjoin.JoinException;
import partjoin.JoinType;
import partjoin.Json;
import partjoin.NestedLoopJoin;
import partjoin.Row;
import partjoin.RowCollector;
import partjoin.Schema;
import partjoin.Table;
import partjoin.Value;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static partjoin.test.Data.row;
import static partjoin.test.Data.table;

/** Full automated suite (plain main(), no JUnit dependency). */
public final class AllTests {

    private static final List<String> LK = List.of("lk");
    private static final List<String> RK = List.of("rk");
    private static final List<String> LK2 = List.of("ka", "kb");
    private static final List<String> RK2 = List.of("ka", "kb");

    public static void main(String[] args) throws Exception {
        Path tmp = Files.createTempDirectory("partjoin-tests");
        List<TestCase> tests = List.of(
                new ValueSemantics(),
                new KeyNullSemantics(),
                new BasicInnerInMemory(),
                new LeftJoinNullPadding(),
                new DuplicatesCartesian(),
                new EmptyTables(),
                new MultiColumnNulls(),
                new RandomCrossChecks(tmp),
                new AllHotKeyFallback(),
                new AllHotKeyLeftFallback(),
                new MixedSkewWithUniform(),
                new LargeScaleSmoke(tmp),
                new RepartitionGenuineSplit(),
                new DiskBudgetExhausted(tmp),
                new JsonRoundTrip(),
                new PlanOnly(),
                new CliEndToEnd(tmp)
        );

        int passed = 0;
        List<TestCase> failed = new ArrayList<>();
        for (TestCase t : tests) {
            boolean ok = t.execute();
            if (ok) {
                passed++;
                System.out.println("PASS  " + t.name);
            } else {
                failed.add(t);
                System.out.println("FAIL  " + t.name);
            }
        }
        System.out.println("-".repeat(60));
        for (TestCase t : failed) {
            System.out.println("FAILED: " + t.name);
            for (String f : t.failures()) System.out.println("    " + f);
            if (t.unexpected() != null) t.unexpected().printStackTrace(System.out);
        }
        System.out.println(passed + "/" + tests.size() + " test classes passed");
        System.exit(failed.isEmpty() ? 0 : 1);
    }

    private static HashJoinEngine.Config cfg(Path tmp, int rows, long budget, int depth) {
        HashJoinEngine.Config c = new HashJoinEngine.Config();
        c.inMemoryRows = rows;
        c.diskBudgetBytes = budget;
        c.maxRepartitionDepth = depth;
        c.spillDir = tmp.resolve("spill").toString();
        return c;
    }

    private static HashJoinEngine engine(Data.TablePair p, JoinType t, HashJoinEngine.Config c) {
        return new HashJoinEngine(p.left(), p.right(), t, LK, RK, c);
    }

    // ------------------------------------------------------------------
    // 1. value semantics: cross-type numeric equality, type-family separation
    // ------------------------------------------------------------------
    static final class ValueSemantics extends TestCase {
        ValueSemantics() {
            super("value equality/hash (numeric cross-type, type families)");
        }

        @Override
        protected void run() {
            check(Value.of(1L).equalsValue(Value.of(1.0d)), "1L == 1.0d");
            check(Value.of(new BigDecimal("1.00")).equalsValue(Value.of(1L)), "1.00 == 1");
            check(Value.of(1.5d).equalsValue(Value.of(new BigDecimal("1.5"))), "1.5d == 1.5");
            check(!Value.of(1L).equalsValue(Value.of("1")), "1 != \"1\"");
            check(!Value.of(1L).equalsValue(Value.of(true)), "1 != true");
            check(!Value.of("a").equalsValue(Value.of("b")), "\"a\" != \"b\"");
            check(!Value.NULL.equalsValue(Value.NULL), "NULL != NULL");
            check(!Value.NULL.equalsValue(Value.of(0L)), "NULL != 0");
            eq(Value.of(1L).hashCode(), Value.of(1.0d).hashCode(), "hash 1L/1.0d");
            eq(Value.of(new BigDecimal("1.00")).hashCode(),
                    Value.of(1L).hashCode(), "hash 1.00/1");
        }
    }

    // ------------------------------------------------------------------
    // 2. composite key NULL semantics
    // ------------------------------------------------------------------
    static final class KeyNullSemantics extends TestCase {
        KeyNullSemantics() {
            super("composite key NULL never matches");
        }

        @Override
        protected void run() {
            Schema s = Schema.of("ka", "kb");
            var i = s.indicesOf(List.of("ka", "kb"));
            var k1 = partjoin.Key.of(row(1, null), i);
            var k2 = partjoin.Key.of(row(1, null), i);
            check(k1.hasNull() && k2.hasNull(), "keys carry null flag");
            check(!k1.equals(k2), "(1,NULL) must not equal (1,NULL)");
            var k3 = partjoin.Key.of(row(1, 2), i);
            var k4 = partjoin.Key.of(row(1, 2), i);
            check(k3.equals(k4) && k3.hashCode() == k4.hashCode(), "(1,2) equals (1,2)");
            var k5 = partjoin.Key.of(row(null, 2), i);
            check(!k5.equals(k4), "(NULL,2) != (1,2)");
        }
    }

    // ------------------------------------------------------------------
    // 3. basic INNER in memory vs oracle
    // ------------------------------------------------------------------
    static final class BasicInnerInMemory extends TestCase {
        BasicInnerInMemory() {
            super("basic INNER join, in-memory");
        }

        @Override
        protected void run() throws Exception {
            Table l = table("l", Data.L2, List.of(row(1, 10), row(2, 20), row(3, 30), row(4, 20)));
            Table r = table("r", Data.R2,
                    List.of(row(10, 10, "a"), row(11, 20, "b"), row(12, 99, "c")));
            var c = cfg(Files.createTempDirectory("pj"), 1024, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            var eo = new HashJoinEngine(l, r, JoinType.INNER, LK, RK, c).execute(out);
            var expected = NestedLoopJoin.join(l, r, JoinType.INNER, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), expected, "basic inner");
            eq(out.rows().size(), 3, "inner row count");
            eq(eo.stats.get("mode"), "IN_MEMORY", "mode");
        }
    }

    // ------------------------------------------------------------------
    // 4. LEFT join null padding, unmatched and null keys
    // ------------------------------------------------------------------
    static final class LeftJoinNullPadding extends TestCase {
        LeftJoinNullPadding() {
            super("LEFT join unmatched + null-key rows padded with NULLs");
        }

        @Override
        protected void run() throws Exception {
            Table l = table("l", Data.L2, List.of(row(1, 10), row(2, 20), row(3, null), row(4, 40)));
            Table r = table("r", Data.R2, List.of(row(10, 10, "a"), row(11, 20, "b")));
            var c = cfg(Files.createTempDirectory("pj"), 1024, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            new HashJoinEngine(l, r, JoinType.LEFT, LK, RK, c).execute(out);
            var expected = NestedLoopJoin.join(l, r, JoinType.LEFT, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), expected, "left null padding");
            eq(out.rows().size(), 4, "left keeps all left rows");
            // row id=3 has null key -> unmatched padding
            boolean nullPadded = out.rows().stream()
                    .anyMatch(x -> x.get(0).raw().equals(3L)
                            && x.get(2).isNull() && x.get(3).isNull() && x.get(4).isNull());
            check(nullPadded, "null-key left row padded with 3 nulls");
            boolean unmatched = out.rows().stream()
                    .anyMatch(x -> x.get(0).raw().equals(4L) && x.get(2).isNull());
            check(unmatched, "unmatched left row padded");
        }
    }

    // ------------------------------------------------------------------
    // 5. duplicates -> Cartesian product
    // ------------------------------------------------------------------
    static final class DuplicatesCartesian extends TestCase {
        DuplicatesCartesian() {
            super("duplicate keys produce Cartesian product");
        }

        @Override
        protected void run() throws Exception {
            Table l = table("l", Data.L2, List.of(row(1, 5), row(2, 5), row(3, 5)));
            Table r = table("r", Data.R2,
                    List.of(row(1, 5, "a"), row(2, 5, "b"), row(3, 6, "c")));
            var c = cfg(Files.createTempDirectory("pj"), 1, Long.MAX_VALUE, 2); // force spill
            var out = new RowCollector.ListCollector();
            var eo = new HashJoinEngine(l, r, JoinType.INNER, LK, RK, c).execute(out);
            var expected = NestedLoopJoin.join(l, r, JoinType.INNER, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), expected, "duplicate cartesian");
            eq(out.rows().size(), 6, "3 x 2 = 6 matches");
            eq(eo.stats.get("mode"), "PARTITIONED_SPILL", "spill forced");
        }
    }

    // ------------------------------------------------------------------
    // 6. empty tables, all four combinations x both join types
    // ------------------------------------------------------------------
    static final class EmptyTables extends TestCase {
        EmptyTables() {
            super("empty left / empty right for INNER and LEFT");
        }

        @Override
        protected void run() throws Exception {
            Table nonEmptyL = table("l", Data.L2, List.of(row(1, 10), row(2, null)));
            Table nonEmptyR = table("r", Data.R2, List.of(row(1, 10, "a")));
            Table emptyL = table("l", Data.L2, List.of());
            Table emptyR = table("r", Data.R2, List.of());

            for (JoinType t : JoinType.values()) {
                cross(nonEmptyL, emptyR, t);
                cross(emptyL, nonEmptyR, t);
                cross(emptyL, emptyR, t);
                cross(nonEmptyL, nonEmptyR, t);
            }
        }

        private void cross(Table l, Table r, JoinType t) throws Exception {
            var c = cfg(Files.createTempDirectory("pj"), 1024, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            new HashJoinEngine(l, r, t, LK, RK, c).execute(out);
            var expected = NestedLoopJoin.join(l, r, t, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), expected,
                    "empty " + t + " l=" + l.rowCount() + " r=" + r.rowCount());
            // LEFT with empty right: every left row padded
            if (t == JoinType.LEFT && r.rowCount() == 0) {
                eq(out.rows().size(), l.rowCount(), "left x empty-right row count");
                for (Row x : out.rows()) {
                    for (int i = l.schema().size(); i < x.size(); i++) {
                        check(x.get(i).isNull(), "padding is null");
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // 7. multi-column keys with NULL in each position
    // ------------------------------------------------------------------
    static final class MultiColumnNulls extends TestCase {
        MultiColumnNulls() {
            super("multi-column keys, NULL in every key position");
        }

        @Override
        protected void run() throws Exception {
            Schema sl = Schema.of("id", "ka", "kb", "payload");
            Schema sr = Schema.of("rid", "ka", "kb", "rv");
            Table l = table("l", sl, List.of(
                    row(1, 1, 1, "a"),
                    row(2, 1, null, "b"),
                    row(3, null, 1, "c"),
                    row(4, null, null, "d"),
                    row(5, 2, 2, "e")));
            Table r = table("r", sr, List.of(
                    row(10, 1, 1, "A"),
                    row(11, 1, null, "B"),
                    row(12, null, 1, "C"),
                    row(13, 2, 2, "D"),
                    row(14, 2, null, "E")));
            var c = cfg(Files.createTempDirectory("pj"), 2, Long.MAX_VALUE, 2); // force spill
            for (JoinType t : JoinType.values()) {
                var out = new RowCollector.ListCollector();
                new HashJoinEngine(l, r, t, LK2, RK2, c).execute(out);
                var expected = NestedLoopJoin.join(l, r, t, LK2, RK2);
                Multiset.assertEqualMultisets(out.rows(), expected, "multi-col " + t);
            }
        }
    }

    // ------------------------------------------------------------------
    // 8. randomized multiset cross-checks across both modes
    // ------------------------------------------------------------------
    static final class RandomCrossChecks extends TestCase {
        private final Path tmp;

        RandomCrossChecks(Path tmp) {
            super("randomized data: engine multiset == nested-loop oracle (in-mem + spill)");
            this.tmp = tmp;
        }

        @Override
        protected void run() throws Exception {
            long seed = 20260923L;
            int scenarios = 0;
            for (JoinType t : JoinType.values()) {
                for (int mode = 0; mode < 3; mode++) {
                    // 0: in-memory, 1: spill+leaf hash, 2: spill+repartition/fallback
                    int ln = mode == 0 ? 50 : 900 + (int) (seed % 300);
                    int rn = mode == 0 ? 40 : 800 + (int) (seed % 400);
                    int domain = mode == 2 ? 20 : 500;
                    int memRows = mode == 0 ? 1024 : (mode == 1 ? 128 : 64);
                    int depth = mode == 2 ? 2 : 2;
                    var pair = Data.randomPair(seed++, ln, rn, domain, 0.08, 1);
                    var c = cfg(tmp, memRows, Long.MAX_VALUE, depth);
                    var out = new RowCollector.ListCollector();
                    var eo = engine(pair, t, c).execute(out);
                    var expected = NestedLoopJoin.join(pair.left(), pair.right(), t, LK, RK);
                    Multiset.assertEqualMultisets(out.rows(), expected,
                            "random " + t + " mode=" + mode + " seed scenario");
                    if (mode > 0) eq(eo.stats.get("mode"), "PARTITIONED_SPILL", "spill mode");
                    scenarios++;
                }
            }
            check(scenarios == 6, "ran 6 scenarios, ran=" + scenarios);
        }
    }

    // ------------------------------------------------------------------
    // 9. ALL hot keys through the bounded fallback (INNER)
    // ------------------------------------------------------------------
    static final class AllHotKeyFallback extends TestCase {
        AllHotKeyFallback() {
            super("all-hot-key INNER: repartition cannot split, BNLJ fallback, full Cartesian");
        }

        @Override
        protected void run() throws Exception {
            Path tmp = Files.createTempDirectory("pj-hot");
            int ln = 3000, rn = 400;
            var pair = Data.allHot(ln, rn);
            var c = cfg(tmp, 64, Long.MAX_VALUE, 2);
            var count = new RowCollector.CountingCollector();
            var eo = engine(pair, JoinType.INNER, c).execute(count);
            eq(count.count(), (long) ln * rn, "all-hot cartesian row count");
            var expected = NestedLoopJoin.join(pair.left(), pair.right(),
                    JoinType.INNER, LK, RK);
            eq((long) expected.size(), (long) ln * rn, "oracle agrees");
            // repartitioning should have been attempted and failed to split,
            // and the bounded fallback must have run
            check(((Number) eo.stats.get("repartitions")).longValue() >= 1,
                    "repartition attempted: " + eo.stats.get("repartitions"));
            eq(((Number) eo.stats.get("fallbackBlockNestedLoops")).longValue(), 1L,
                    "exactly one fallback");
            eq(eo.stats.get("mode"), "PARTITIONED_SPILL", "mode");

            // Multiset spot check with a smaller all-hot case (payloads distinguish rows).
            var small = Data.allHot(300, 150);
            var c2 = cfg(Files.createTempDirectory("pj-hot2"), 16, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            engine(small, JoinType.INNER, c2).execute(out);
            var exp = NestedLoopJoin.join(small.left(), small.right(), JoinType.INNER, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), exp, "all-hot multiset");
        }
    }

    // ------------------------------------------------------------------
    // 10. ALL hot keys LEFT through fallback: every left row matches
    // ------------------------------------------------------------------
    static final class AllHotKeyLeftFallback extends TestCase {
        AllHotKeyLeftFallback() {
            super("all-hot-key LEFT via fallback + BNLJ marks every probe row");
        }

        @Override
        protected void run() throws Exception {
            Path tmp = Files.createTempDirectory("pj-hotleft");
            // left hot + a few unmatched/null rows interspersed
            List<Row> ls = new ArrayList<>();
            List<Row> rs = new ArrayList<>();
            for (int i = 0; i < 2500; i++) ls.add(row(i, 7, "L" + i));
            ls.add(row(9001, 8, "unmatched"));
            ls.add(row(9002, null, "nullkey"));
            for (int i = 0; i < 300; i++) rs.add(row(i, 7, "R" + i));
            Table l = table("l", Schema.of("id", "lk", "lp"), ls);
            Table r = table("r", Data.R2, rs);
            var c = cfg(tmp, 64, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            var eo = new HashJoinEngine(l, r, JoinType.LEFT, LK, RK, c).execute(out);
            var expected = NestedLoopJoin.join(l, r, JoinType.LEFT, LK, RK);
            Multiset.assertEqualMultisets(out.rows(), expected, "hot left multiset");
            eq(out.rows().size(), 2500L * 300 + 2, "2500x300 matches + 2 unmatched");
            check(((Number) eo.stats.get("fallbackBlockNestedLoops")).longValue() >= 1,
                    "fallback used");
        }
    }

    // ------------------------------------------------------------------
    // 10b. mixed skew: one hot key among many uniform keys; hot partition
    //      falls back while the rest split - whole result still matches oracle
    // ------------------------------------------------------------------
    static final class MixedSkewWithUniform extends TestCase {
        MixedSkewWithUniform() {
            super("mixed skew: hot key + uniform keys, fallback and split coexist");
        }

        @Override
        protected void run() throws Exception {
            Path tmp = Files.createTempDirectory("pj-mixed");
            var pair = Data.mixedSkew(7L, 4000, 4000, 3000);
            var c = cfg(tmp, 64, Long.MAX_VALUE, 2);
            for (JoinType t : JoinType.values()) {
                var out = new RowCollector.ListCollector();
                var eo = engine(pair, t, c).execute(out);
                var expected = NestedLoopJoin.join(pair.left(), pair.right(), t, LK, RK);
                Multiset.assertEqualMultisets(out.rows(), expected, "mixed skew " + t);
                check(((Number) eo.stats.get("fallbackBlockNestedLoops")).longValue() >= 1,
                        t + ": hot partition used fallback");
                check(((Number) eo.stats.get("repartitions")).longValue() >= 1,
                        t + ": repartition attempted");
            }
        }
    }

    // ------------------------------------------------------------------
    // 10c. larger scale smoke: 50k x 50k unique keys with bounded memory
    // ------------------------------------------------------------------
    static final class LargeScaleSmoke extends TestCase {
        private final Path tmp;

        LargeScaleSmoke(Path tmp) {
            super("50k x 50k unique-key INNER smoke (bounded memory)");
            this.tmp = tmp;
        }

        @Override
        protected void run() throws Exception {
            int n = 50_000;
            List<Row> ls = new ArrayList<>();
            List<Row> rs = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                ls.add(row(i, i, "L"));
                rs.add(row(i, i, "R"));
            }
            Table l = table("l", Schema.of("id", "lk", "lp"), ls);
            Table r = table("r", Data.R2, rs);
            var c = cfg(tmp, 512, Long.MAX_VALUE, 2);
            var count = new RowCollector.CountingCollector();
            long t0 = System.currentTimeMillis();
            var eo = new HashJoinEngine(l, r, JoinType.INNER, LK, RK, c).execute(count);
            long ms = System.currentTimeMillis() - t0;
            eq(count.count(), n, "50k unique pairs");
            eq(((Number) eo.stats.get("fallbackBlockNestedLoops")).longValue(), 0L, "no fallback");
            check(ms < 60_000, "completes within 60s, took " + ms + "ms");
            System.out.println("    (50k smoke took " + ms + " ms, "
                    + eo.stats.get("spillBytesWritten") + " spill bytes, "
                    + eo.stats.get("partitionsCreated") + " partitions)");
        }
    }

    // ------------------------------------------------------------------
    // 11. genuine repartition split: uniform large keys shrink, fallback NOT used
    // ------------------------------------------------------------------
    static final class RepartitionGenuineSplit extends TestCase {
        RepartitionGenuineSplit() {
            super("uniform keys repartition into smaller partitions without fallback");
        }

        @Override
        protected void run() throws Exception {
            Path tmp = Files.createTempDirectory("pj-split");
            int ln = 6000, rn = 6000;
            List<Row> ls = new ArrayList<>();
            List<Row> rs = new ArrayList<>();
            for (int i = 0; i < ln; i++) ls.add(row(i, i, "L" + i));
            for (int i = 0; i < rn; i++) rs.add(row(i, i, "R" + i));
            Table l = table("l", Schema.of("id", "lk", "lp"), ls);
            Table r = table("r", Data.R2, rs);
            var c = cfg(tmp, 32, Long.MAX_VALUE, 2);
            var out = new RowCollector.ListCollector();
            var eo = new HashJoinEngine(l, r, JoinType.INNER, LK, RK, c).execute(out);
            eq(out.rows().size(), 6000, "unique-key join output");
            // Output layout: [id, lk, lp, rid, rk, rv] - matching pairs share id == rid
            for (Row x : out.rows()) {
                eq(x.get(0).raw(), x.get(3).raw(), "matching ids paired");
            }
            eq(((Number) eo.stats.get("fallbackBlockNestedLoops")).longValue(), 0L,
                    "no fallback for uniform keys");
            check(((Number) eo.stats.get("repartitions")).longValue() >= 1,
                    "repartition happened: " + eo.stats.get("repartitions"));
        }
    }

    // ------------------------------------------------------------------
    // 12. disk budget exhausted -> DISK_BUDGET_EXHAUSTED, spill dir cleaned
    // ------------------------------------------------------------------
    static final class DiskBudgetExhausted extends TestCase {
        private final Path tmp;

        DiskBudgetExhausted(Path tmp) {
            super("disk quota exhausted raises DISK_BUDGET_EXHAUSTED and cleans up");
            this.tmp = tmp;
        }

        @Override
        protected void run() throws Exception {
            Path spill = tmp.resolve("budget-spill");
            var pair = Data.allHot(5000, 5000);
            var c = new HashJoinEngine.Config();
            c.inMemoryRows = 64;
            c.diskBudgetBytes = 2048;
            c.maxRepartitionDepth = 2;
            c.spillDir = spill.toString();
            var count = new RowCollector.CountingCollector();
            JoinException ex = null;
            try {
                engine(pair, JoinType.INNER, c).execute(count);
            } catch (JoinException e) {
                ex = e;
            }
            check(ex != null, "budget exception thrown");
            eq(ex.code(), JoinException.DISK_BUDGET_EXHAUSTED, "error code");
            eq(count.count(), 0, "no output before the initial spill completes");
            // The configured spillDir may remain as an empty parent, but every
            // unique per-run subdirectory and spill file must be cleaned up.
            if (Files.exists(spill) && Files.isDirectory(spill)) {
                try (var walk = Files.walk(spill)) {
                    var all = walk.toList();
                    var leftoverFiles = all.stream().filter(Files::isRegularFile).toList();
                    if (!leftoverFiles.isEmpty()) throw new AssertionError(
                            "spill files leaked: " + leftoverFiles);
                    var leftoverEntries = all.stream()
                            .filter(p -> !p.equals(spill)).toList();
                    if (!leftoverEntries.isEmpty()) throw new AssertionError(
                            "leftover entries: " + leftoverEntries.stream()
                                    .map(String::valueOf).toList());
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // 13. JSON round trip (parser/writer fidelity incl. BigDecimal/unicode)
    // ------------------------------------------------------------------
    static final class JsonRoundTrip extends TestCase {
        JsonRoundTrip() {
            super("JSON parse/write fidelity");
        }

        @Override
        protected void run() {
            String text = "{\"a\":1,\"b\":1.50,\"big\":99999999999999999999,"
                    + "\"neg\":-7,\"s\":\"x\\n\\u00e9\",\"t\":true,\"f\":false,"
                    + "\"n\":null,\"arr\":[1,2,3.25,\"q\"]}";
            Map<String, Object> m = Json.parseObject(text);
            eq(m.get("a"), 1L, "int -> Long");
            check(m.get("b") instanceof BigDecimal && ((BigDecimal) m.get("b")).signum() > 0,
                    "decimal -> BigDecimal");
            eq(m.get("s"), "x\n" + "é", "escapes/unicode");
            check(m.get("n") == null, "null");
            String again = Json.write(m);
            Map<String, Object> m2 = Json.parseObject(again);
            eq(m2.get("a"), 1L, "rewrite int");
            eq(m2.get("s"), "x\n" + "é", "rewrite string");
            // malformed input must be rejected
            boolean threw = false;
            try {
                Json.parse("{bad");
            } catch (JoinException e) {
                threw = true;
            }
            check(threw, "malformed JSON rejected");
        }
    }

    // ------------------------------------------------------------------
    // 14. plan-only endpoint reports modes and does not execute
    // ------------------------------------------------------------------
    static final class PlanOnly extends TestCase {
        PlanOnly() {
            super("explain/plan-only reports IN_MEMORY vs PARTITIONED_SPILL");
        }

        @Override
        protected void run() throws Exception {
            var small = Data.randomPair(1, 10, 10, 5, 0, 0);
            var c = cfg(Files.createTempDirectory("pj-plan"), 1024, Long.MAX_VALUE, 2);
            var plan = new HashJoinEngine(small.left(), small.right(), JoinType.INNER,
                    LK, RK, c).explain();
            eq(plan.get("mode"), "IN_MEMORY", "small plan mode");

            var big = Data.randomPair(2, 5000, 5000, 100, 0, 0);
            var c2 = cfg(Files.createTempDirectory("pj-plan2"), 64, Long.MAX_VALUE, 2);
            var plan2 = new HashJoinEngine(big.left(), big.right(), JoinType.INNER,
                    LK, RK, c2).explain();
            eq(plan2.get("mode"), "PARTITIONED_SPILL", "big plan mode");
            check(((Number) plan2.get("fanout")).intValue() >= 4, "fanout >= 4");
        }
    }

    // ------------------------------------------------------------------
    // 15. end-to-end CLI: run + plan + quota failure, via the JSON entry point
    // ------------------------------------------------------------------
    static final class CliEndToEnd extends TestCase {
        private final Path tmp;

        CliEndToEnd(Path tmp) {
            super("CLI end-to-end: run, plan-only, DISK_BUDGET_EXHAUSTED exit code");
            this.tmp = tmp;
        }

        @Override
        protected void run() throws Exception {
            Path classes = Path.of("build/classes");
            check(Files.isDirectory(classes), "build/classes present");

            // successful run via stdin
            String request = """
                    {
                      "joinType": "INNER",
                      "left":  {"name":"l","columns":["id","lk"],"rows":[[1,10],[2,20],[3,null]]},
                      "right": {"name":"r","columns":["rid","rk","rv"],"rows":[[100,10,"a"],[101,20,"b"]]},
                      "leftKeys": ["lk"],
                      "rightKeys": ["rk"],
                      "options": {"inMemoryRows": 2}
                    }
                    """;
            String resp = runJava(classes, request, "partjoin.Main", "-");
            Map<String, Object> doc = Json.parseObject(resp);
            eq(doc.get("ok"), true, "CLI ok");
            Map<String, Object> stats = Json.getObject(doc, "stats");
            eq(stats.get("outputRows"), 2L, "CLI output rows");
            eq(stats.get("mode"), "PARTITIONED_SPILL", "CLI spills");

            // plan-only
            Path reqFile = tmp.resolve("req.json");
            Files.writeString(reqFile, request.replace("\"inMemoryRows\": 2", "\"inMemoryRows\": 1024"));
            String planResp = runJava(classes, null, "partjoin.Main", "plan", reqFile.toString());
            Map<String, Object> planDoc = Json.parseObject(planResp);
            eq(planDoc.get("ok"), true, "plan ok");
            eq(Json.getObject(planDoc, "plan").get("mode"), "IN_MEMORY", "plan mode");

            // quota exhausted -> exit code 2 with an error envelope on stderr
            String bad = request.replace("\"inMemoryRows\": 2",
                    "\"inMemoryRows\": 2, \"diskBudgetBytes\": 100");
            ProcResult pr = runJavaCapture(classes, bad, "partjoin.Main", "-");
            eq(pr.exitCode, 2, "exit code 2 on disk exhaustion");
            Map<String, Object> errDoc = Json.parseObject(pr.stderr);
            eq(errDoc.get("ok"), false, "error envelope");
            eq(Json.getObject(errDoc, "error").get("code"),
                    JoinException.DISK_BUDGET_EXHAUSTED, "error code via CLI");
        }

        private String runJava(Path cp, String stdin, String mainClass, String... args) throws Exception {
            return runJavaCapture(cp, stdin, mainClass, args).stdout;
        }

        private ProcResult runJavaCapture(Path cp, String stdin, String mainClass, String... args)
                throws Exception {
            List<String> cmd = new ArrayList<>(List.of(
                    System.getProperty("java.home") + "/bin/java",
                    "-cp", cp.toString(), mainClass));
            cmd.addAll(List.of(args));
            ProcessBuilder pb = new ProcessBuilder(cmd);
            Process p = pb.start();
            if (stdin != null) {
                p.getOutputStream().write(stdin.getBytes(StandardCharsets.UTF_8));
                p.getOutputStream().close();
            }
            StringBuilder out = new StringBuilder();
            StringBuilder err = new StringBuilder();
            try (BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream(),
                    StandardCharsets.UTF_8))) {
                br.lines().forEach(l -> out.append(l).append('\n'));
            }
            try (BufferedReader br = new BufferedReader(new InputStreamReader(p.getErrorStream(),
                    StandardCharsets.UTF_8))) {
                br.lines().forEach(l -> err.append(l).append('\n'));
            }
            int code = p.waitFor();
            return new ProcResult(code, out.toString(), err.toString());
        }

        record ProcResult(int exitCode, String stdout, String stderr) {
        }
    }
}
