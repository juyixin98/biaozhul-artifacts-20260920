package joinplanner.sim;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import joinplanner.core.DpResult;
import joinplanner.core.JoinPlanner;
import joinplanner.core.ProblemParser;
import joinplanner.core.Stats;
import joinplanner.core.ValidatedProblem;

/**
 * Full estimation-vs-reality comparison:
 * <ol>
 *   <li>parse and validate the same request shape as /api/plan;</li>
 *   <li>generate deterministic, possibly Zipf-skewed data for every edge column;</li>
 *   <li>execute joins for real ({@link JoinExecutor}) and record exact
 *       intermediate cardinalities for every table subset;</li>
 *   <li>score every legal left-deep order with both the estimator's cardinalities
 *       and the measured ones, so the estimated-best and true-best orders can
 *       diverge under skew.</li>
 * </ol>
 */
public final class Simulator {

    /** One order scored under both models. */
    public record OrderComparison(
            List<String> order,
            boolean legal,
            double estimatedCost,
            long trueCost,
            double estRanking,
            long trueRanking) {
    }

    /** Result bundle. */
    public record SimResult(
            ValidatedProblem problem,
            DpResult estimatedPlan,
            List<String> estimatedJoinOrder,
            double estimatedPlanCost,
            List<String> trueBestOrder,
            long trueBestCost,
            boolean ordersAgree,
            List<OrderComparison> orders,
            Map<String, long[]> subsetCards,
            long seed) {
    }

    private static final int MAX_REPORT_ORDERS = 50;

    public SimResult run(SimConfig cfg) {
        ValidatedProblem problem = new ProblemParser().parse(cfg.problemJson());

        Map<String, TableData> data = generate(problem, cfg.columns(), cfg.seed());
        JoinExecutor exec = new JoinExecutor(problem, data, cfg.maxRows());

        int n = problem.n();
        int size = 1 << n;
        long[] exact = new long[size];
        double[] estLogCard = estimatedSubsetCards(problem);

        // Subsets for a connected graph; the estimator works on every mask.
        for (int mask = 1; mask < size; mask++) {
            if (connected(problem, mask)) {
                exact[mask] = exec.exactCardinality(mask);
            }
        }

        // Enumerate every permutation, score legal left-deep orders both ways.
        List<int[]> perms = new ArrayList<>();
        permutations(n, perms);

        List<Scored> scored = new ArrayList<>();
        for (int[] perm : perms) {
            int mask = 1 << perm[0];
            boolean legal = true;
            double estLogCost = Double.NEGATIVE_INFINITY;
            long trueCost = 0L;
            for (int k = 1; k < n; k++) {
                int t = perm[k];
                if (!edgeTo(t, mask, problem)) {
                    legal = false;
                    break;
                }
                mask |= 1 << t;
                estLogCost = Stats.logAdd(estLogCost, estLogCard[mask]);
                trueCost = addSaturated(trueCost, exact[mask]);
            }
            if (legal) {
                List<String> names = new ArrayList<>();
                for (int t : perm) {
                    names.add(problem.spec().tables().get(t).name());
                }
                scored.add(new Scored(names, Stats.fromLog(estLogCost), trueCost));
            }
        }

        // Estimator-side plan (full bushy DP).
        JoinPlanner planner = new JoinPlanner(problem);
        DpResult dp = planner.plan();

        // Rankings.
        List<Scored> byEst = new ArrayList<>(scored);
        byEst.sort(Comparator.comparingDouble((Scored s) -> s.estCost)
                .thenComparing(s -> String.join(",", s.order)));
        List<Scored> byTrue = new ArrayList<>(scored);
        byTrue.sort(Comparator.comparingLong((Scored s) -> s.trueCost)
                .thenComparing(s -> String.join(",", s.order)));

        Map<String, Long> estRank = new LinkedHashMap<>();
        Map<String, Long> trueRank = new LinkedHashMap<>();
        assignRanks(byEst, estRank, false);
        assignRanks(byTrue, trueRank, true);

        List<Scored> report = scored.size() <= MAX_REPORT_ORDERS
                ? byTrue : byTrue.subList(0, MAX_REPORT_ORDERS);

        List<OrderComparison> orders = new ArrayList<>();
        for (Scored s : report) {
            String key = String.join(",", s.order);
            orders.add(new OrderComparison(
                    List.copyOf(s.order), true, s.estCost, s.trueCost,
                    estRank.get(key), trueRank.get(key)));
        }

        Scored trueBest = byTrue.get(0);
        Scored estBest = byEst.get(0);

        // DP (bushy) join order flattened into a left-deep-like name sequence for
        // display; the order the estimator recommends among LEFT-DEEP plans is the
        // permutation rank 1, which is what skew comparison is about.
        boolean agree = estBest.order.equals(trueBest.order);

        Map<String, long[]> subsetOut = new LinkedHashMap<>();
        for (int mask = 1; mask < size; mask++) {
            if (connected(problem, mask)) {
                List<String> names = new ArrayList<>();
                for (int i = 0; i < n; i++) {
                    if ((mask & (1 << i)) != 0) {
                        names.add(problem.spec().tables().get(i).name());
                    }
                }
                double est = Stats.fromLog(estLogCard[mask]);
                subsetOut.put(String.join("⨝", names),
                        new long[]{(long) Math.rint(est), exact[mask]});
            }
        }

        return new SimResult(
                problem, dp,
                List.copyOf(estBest.order), estBest.estCost,
                List.copyOf(trueBest.order), trueBest.trueCost,
                agree, List.copyOf(orders), subsetOut, cfg.seed());
    }

    private record Scored(List<String> order, double estCost, long trueCost) {
    }

    private void assignRanks(List<Scored> sorted, Map<String, Long> out, boolean byTrueKey) {
        long rank = 1;
        for (int i = 0; i < sorted.size(); i++) {
            Scored s = sorted.get(i);
            boolean tie = i > 0 && (byTrueKey
                    ? s.trueCost == sorted.get(i - 1).trueCost
                    : s.estCost == sorted.get(i - 1).estCost);
            if (!tie) {
                rank = i + 1L;
            }
            out.put(String.join(",", s.order), rank);
        }
    }

    private static long addSaturated(long a, long b) {
        long s = a + b;
        return s < 0 ? Long.MAX_VALUE : s;
    }

    private double[] estimatedSubsetCards(ValidatedProblem p) {
        JoinPlanner planner = new JoinPlanner(p);
        planner.plan();
        int size = 1 << p.n();
        double[] log = new double[size];
        for (int m = 0; m < size; m++) {
            log[m] = planner.subsetLogCardinality(m);
        }
        return log;
    }

    private Map<String, TableData> generate(ValidatedProblem problem,
                                            List<ColumnGen> overrides, long seed) {
        Map<String, Map<String, ColumnGen>> cfg = new LinkedHashMap<>();
        for (ColumnGen c : overrides) {
            cfg.computeIfAbsent(c.table(), k -> new LinkedHashMap<>()).put(c.column(), c);
        }

        // Collect every (table, column) referenced by edges.
        Map<String, java.util.Set<String>> needed = new LinkedHashMap<>();
        for (var e : problem.resolvedEdges()) {
            var s = e.spec();
            needed.computeIfAbsent(s.leftTable(), k -> new java.util.LinkedHashSet<>())
                    .add(s.leftColumn());
            needed.computeIfAbsent(s.rightTable(), k -> new java.util.LinkedHashSet<>())
                    .add(s.rightColumn());
        }

        Random rng = new Random(seed);
        Map<String, TableData> out = new LinkedHashMap<>();
        for (int ti = 0; ti < problem.n(); ti++) {
            var table = problem.spec().tables().get(ti);
            long rows = (long) table.rows();
            if (table.rows() != rows) {
                throw new joinplanner.core.BadRequestException(
                        "simulation: table '" + table.name()
                                + "' rows must be an integer, got " + table.rows());
            }
            TableData td = new TableData(table.name());
            for (String col : needed.getOrDefault(table.name(), java.util.Set.of())) {
                ColumnGen gen = cfg.getOrDefault(table.name(), Map.of()).get(col);
                long ndv = gen != null && gen.ndv() > 0 ? gen.ndv() : rows;
                double zipf = gen != null ? gen.zipf() : 0.0;
                if (ndv <= 0) {
                    throw new joinplanner.core.BadRequestException(
                            "simulation: ndv for " + table.name() + "." + col
                                    + " must be positive");
                }
                double[] weights = DataGenerator.cumulativeWeights(ndv, zipf);
                int[] vals = new int[(int) rows];
                for (int r = 0; r < rows; r++) {
                    vals[r] = DataGenerator.sample(weights, rng);
                }
                td.putColumn(col, vals);
            }
            // tables with no referenced columns still need one column for row count
            if (td.columns().isEmpty()) {
                td.putColumn("_scan", new int[(int) rows]);
            }
            out.put(table.name(), td);
        }
        return out;
    }

    private boolean connected(ValidatedProblem p, int mask) {
        int start = Integer.numberOfTrailingZeros(mask);
        int reached = 1 << start;
        int frontier = reached;
        while (frontier != 0) {
            int i = Integer.numberOfTrailingZeros(frontier);
            frontier &= frontier - 1;
            for (int[] nb : p.adjacency().get(i)) {
                int bit = 1 << nb[0];
                if ((mask & bit) != 0 && (reached & bit) == 0) {
                    reached |= bit;
                    frontier |= bit;
                }
            }
        }
        return reached == mask;
    }

    private boolean edgeTo(int table, int mask, ValidatedProblem p) {
        for (int[] nb : p.adjacency().get(table)) {
            if ((mask & (1 << nb[0])) != 0) {
                return true;
            }
        }
        return false;
    }

    private void permutations(int n, List<int[]> out) {
        int[] a = new int[n];
        for (int i = 0; i < n; i++) {
            a[i] = i;
        }
        collect(a, 0, out);
    }

    private void collect(int[] a, int k, List<int[]> out) {
        if (k == a.length) {
            out.add(a.clone());
            return;
        }
        for (int i = k; i < a.length; i++) {
            int t = a[k];
            a[k] = a[i];
            a[i] = t;
            collect(a, k + 1, out);
            t = a[k];
            a[k] = a[i];
            a[i] = t;
        }
    }
}
