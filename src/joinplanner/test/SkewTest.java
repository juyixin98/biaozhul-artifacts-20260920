package joinplanner.test;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

import joinplanner.core.Stats;
import joinplanner.json.JsonParser;
import joinplanner.sim.ColumnGen;
import joinplanner.sim.JoinExecutor;
import joinplanner.sim.SimConfig;
import joinplanner.sim.Simulator;
import joinplanner.sim.TableData;

/**
 * The acceptance core: build skewed data, compare estimator cardinalities with
 * measured intermediate row counts, and confirm estimated optimum can differ
 * from the true optimum.
 */
public final class SkewTest {

    public static void run(TestRunner r) {
        r.suite("Executor sanity: tiny hand-checkable join");
        {
            // a(x): [0,0,1]  b(y): [0,0,0]  => join = 6 rows
            var problem = TestBase.builder(3, 3).edge(0, 1, 0.5).build();
            TableData a = new TableData("t0");
            a.putColumn("c", new int[]{0, 0, 1});
            TableData b = new TableData("t1");
            b.putColumn("c", new int[]{0, 0, 0});
            JoinExecutor exec = new JoinExecutor(problem, Map.of("t0", a, "t1", b), 1_000_000);
            r.checkEq("2-table exact join rows", exec.exactCardinality(0b11), 6L);
            // estimator with sel .5 says 3*3*.5 = 4.5
            JoinExecutor exec2 = exec;
            double est = new JoinPlannerCost(problem).card(0b11);
            r.checkClose("estimator says 4.5", est, 4.5, 1e-12);
            r.check("estimation differs from reality", Math.abs(est - 6) > 1.0);
        }

        r.suite("Executor sanity: 3-table chain hand-checked");
        {
            // a [0,1], b [0,1], c [0,0]
            // a=b => (0,0),(1,1) 2 rows; then =c(0): (a0,b0) matches twice => 2
            var problem = TestBase.builder(2, 2, 2)
                    .edge(0, 1, 0.5).edge(1, 2, 0.5).build();
            Map<String, TableData> data = Map.of(
                    "t0", table("t0", new int[]{0, 1}),
                    "t1", table("t1", new int[]{0, 1}),
                    "t2", table("t2", new int[]{0, 0}));
            JoinExecutor exec = new JoinExecutor(problem, data, 1_000_000);
            r.checkEq("a-b exact", exec.exactCardinality(0b011), 2L);
            r.checkEq("b-c exact", exec.exactCardinality(0b110), 2L);
            r.checkEq("a-b-c exact", exec.exactCardinality(0b111), 2L);
        }

        r.suite("Skewed Zipf data: estimated vs true optimum can diverge");
        boolean anyDiverge = false;
        for (long seed : new long[]{1L, 7L, 42L, 99L, 1234L}) {
            anyDiverge |= runSkewDivergence(r, seed);
        }
        r.check("at least one data seed makes estimated optimum != true optimum",
                anyDiverge);
    }

    /** @return true if the estimated and true optimal orders DIVERGE */
    private static boolean runSkewDivergence(TestRunner r, long seed) {
        // 4 tables in a chain t0--t1--t2--t3. Give the estimator the base
        // cardinalities but NO ndv/selectivity => it assumes unique keys and
        // believes each join ~ min-side rows. Actual columns are Zipf-skewed
        // with small NDV, so intermediate sizes depend on order.
        String problemJson = """
                {
                  "tables": [
                    {"name": "t0", "rows": 120},
                    {"name": "t1", "rows": 200},
                    {"name": "t2", "rows": 150},
                    {"name": "t3", "rows": 80}
                  ],
                  "edges": [
                    {"leftTable": "t0", "leftColumn": "k01",
                     "rightTable": "t1", "rightColumn": "k10"},
                    {"leftTable": "t1", "leftColumn": "k12",
                     "rightTable": "t2", "rightColumn": "k21"},
                    {"leftTable": "t2", "leftColumn": "k23",
                     "rightTable": "t3", "rightColumn": "k32"}
                  ]
                }
                """;
        Object parsed = JsonParser.parse(problemJson);

        List<ColumnGen> cols = new ArrayList<>();
        // heavy skew on two edges, lighter on the third, mismatched NDVs
        cols.add(new ColumnGen("t0", "k01", 15, 1.4));
        cols.add(new ColumnGen("t1", "k10", 25, 0.3));
        cols.add(new ColumnGen("t1", "k12", 12, 1.5));
        cols.add(new ColumnGen("t2", "k21", 30, 0.2));
        cols.add(new ColumnGen("t2", "k23", 20, 0.4));
        cols.add(new ColumnGen("t3", "k32", 8, 1.3));

        Simulator.SimResult result = new Simulator()
                .run(new SimConfig(parsed, cols, seed, 5_000_000));

        // 1) measured cardinalities are integers and plausibly off the estimate
        boolean someSubsetMispredicted = result.subsetCards().values().stream()
                .anyMatch(pair -> {
                    long est = pair[0];
                    long act = pair[1];
                    if (act == 0) {
                        return est != 0;
                    }
                    double ratio = (double) est / act;
                    return ratio < 0.66 || ratio > 1.5;
                });
        r.check("seed " + seed + ": at least one subset cardinality mispredicted (>50%)",
                someSubsetMispredicted);

        // 2) report contains all legal left-deep orders (chain: 2^(n-1)=8)
        long legalOrders = result.orders().size();
        r.check("seed " + seed + ": all 8 legal chain orders scored", legalOrders == 8);

        // 3) the true-best order's measured cost is really minimal
        long minTrue = result.orders().stream()
                .mapToLong(Simulator.OrderComparison::trueCost).min().orElseThrow();
        r.checkEq("seed " + seed + ": trueBestCost equals enumeration minimum",
                result.trueBestCost(), minTrue);

        // 4) verify true costs by independent subset-cardinality recomputation:
        //    cost(order) = sum exact[prefix]
        boolean prefixConsistent = verifyPrefixCosts(result, cols, seed);
        r.check("seed " + seed + ": order costs consistent with exact subset cards",
                prefixConsistent);

        System.out.println("        seed " + seed + " estimatedBest="
                + result.estimatedJoinOrder() + " (estCost="
                + fmt(result.estimatedPlanCost()) + ")  trueBest="
                + result.trueBestOrder() + " (trueCost=" + result.trueBestCost()
                + ")  agree=" + result.ordersAgree());
        return !result.ordersAgree();
    }

    private static boolean verifyPrefixCosts(Simulator.SimResult result,
                                             List<ColumnGen> cols, long seed) {
        // Re-derive the exact subset map from the reported table and recompute each
        // reported order cost from reported per-subset actual rows.
        var p = result.problem();
        java.util.Map<String, Long> byName = new java.util.HashMap<>();
        result.subsetCards().forEach((k, v) -> byName.put(k, v[1]));
        for (var o : result.orders()) {
            long sum = 0;
            List<String> prefix = new ArrayList<>();
            for (String t : o.order()) {
                prefix.add(t);
                if (prefix.size() >= 2) {
                    // subset keys are named in table-index order, so sort the prefix
                    List<String> key = new ArrayList<>(prefix);
                    key.sort(Comparator.comparingInt(name ->
                            tableIndex(result, name)));
                    Long rows = byName.get(String.join("⨝", key));
                    if (rows == null) {
                        return false;
                    }
                    sum += rows;
                }
            }
            if (sum != o.trueCost()) {
                return false;
            }
        }
        return true;
    }

    private static int tableIndex(Simulator.SimResult result, String name) {
        for (int i = 0; i < result.problem().n(); i++) {
            if (result.problem().spec().tables().get(i).name().equals(name)) {
                return i;
            }
        }
        throw new IllegalStateException(name);
    }

    private static TableData table(String name, int[] col) {
        TableData t = new TableData(name);
        t.putColumn("c", col);
        return t;
    }

    private static String fmt(double d) {
        return String.format(java.util.Locale.ROOT, "%.4g", d);
    }

    /** Small adapter to read a subset estimate in tests. */
    private static final class JoinPlannerCost {
        private final joinplanner.core.JoinPlanner planner;

        JoinPlannerCost(joinplanner.core.ValidatedProblem p) {
            this.planner = new joinplanner.core.JoinPlanner(p);
            planner.plan();
        }

        double card(int mask) {
            return Stats.fromLog(planner.subsetLogCardinality(mask));
        }
    }
}
