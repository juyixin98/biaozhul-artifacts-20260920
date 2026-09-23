package joinplanner;

import joinplanner.sim.DataGenerator;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

public class TestSimulation {

    public static void main(String[] args) {
        TestFramework t = new TestFramework();
        testDistributions(t);
        testUniformAgreement(t);
        testSkewDivergence(t);
        testExplicitSkewScenario(t);
        testCapping(t);
        if (t.failures.isEmpty()) {
            System.out.println("PASS TestSimulation (" + t.checks() + " checks)");
        } else {
            System.out.println("FAIL TestSimulation");
            t.failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }

    static void testDistributions(TestFramework t) {
        // Sequence column is unique.
        joinplanner.sim.ColumnSpec seq =
                new joinplanner.sim.ColumnSpec("sequence", -1, 1, 0.5, null, true, 0);
        long[] vals = DataGenerator.generate(seq, 500, new Random(1));
        t.eq(vals.length, 500, "sequence row count");
        t.eq(vals[0], 1L, "sequence starts at 1");
        t.eq(vals[499], 500L, "sequence ends at rows");
        t.eq(DataGenerator.actualNdv(vals), 500L, "sequence NDV = rows");

        // Offset shifts every value.
        joinplanner.sim.ColumnSpec off =
                new joinplanner.sim.ColumnSpec("uniform", 10, 1, 0.5, null, false, 1000);
        long[] ov = DataGenerator.generate(off, 100, new Random(2));
        long min = Long.MAX_VALUE, max = Long.MIN_VALUE;
        for (long v : ov) {
            min = Math.min(min, v);
            max = Math.max(max, v);
        }
        t.check(min >= 1001 && max <= 1010, "offset shifts value domain (got " + min + ".." + max + ")");

        // Hot key: value 1 dominates.
        joinplanner.sim.ColumnSpec hot =
                new joinplanner.sim.ColumnSpec("hotkey", 100, 1, 0.8, null, false, 0);
        long[] hv = DataGenerator.generate(hot, 10_000, new Random(3));
        long ones = 0;
        for (long v : hv) {
            if (v == 1) ones++;
        }
        t.check(ones > 7000 && ones < 9000, "hotkey concentrates ~80% on value 1 (got " + ones + ")");
    }

    /** With uniform columns the NDV/uniformity estimator should be close to actual rows. */
    @SuppressWarnings("unchecked")
    static void testUniformAgreement(TestFramework t) {
        String json = """
                {"seed":7,
                 "tables":[
                   {"name":"A","rows":5000,"columns":[{"distribution":"uniform","ndv":500}]},
                   {"name":"B","rows":8000,"columns":[{"distribution":"uniform","ndv":500},
                                                       {"distribution":"uniform","ndv":200}]},
                   {"name":"C","rows":4000,"columns":[{"distribution":"uniform","ndv":200}]}],
                 "edges":[
                   {"left":"A","right":"B","leftColumn":0,"rightColumn":0},
                   {"left":"B","right":"C","leftColumn":1,"rightColumn":0}]}
                """;
        Map<String, Object> resp = TestSupport.simulate(json);
        List<Map<String, Object>> rows = (List<Map<String, Object>>) resp.get("intermediateRows");
        for (Map<String, Object> row : rows) {
            double est = ((Number) row.get("estimatedRows")).doubleValue();
            double act = ((Number) row.get("actualRows")).doubleValue();
            List<?> tables = (List<?>) row.get("tables");
            if (tables.size() == 3) {
                double ratio = est / act;
                t.check(ratio > 0.5 && ratio < 2.0,
                        "uniform 3-way join estimate within 2x of actual (ratio="
                                + String.format("%.3f", ratio) + ")");
            }
        }
        Map<String, Object> cmp = (Map<String, Object>) resp.get("comparison");
        t.eq(cmp.get("plansAgree"), true,
                "uniform data: estimated and true optimal plan agree");
    }

    /**
     * Constructs a scenario where the estimator is deliberately fooled:
     * <ul>
     *   <li>A and B share the <b>same Zipf hot keys</b> → real A⋈B is much larger than
     *       the NDV estimate predicts.</li>
     *   <li>B's join column to C lives in a shifted value domain → real B⋈C is (nearly)
     *       empty, much smaller than estimated.</li>
     *   <li>Explicit selectivities make the estimator believe A⋈B is the cheap edge, so
     *       it builds that side first; measured cardinalities then flip the order.</li>
     * </ul>
     * This is the canonical "estimated optimum ≠ true optimum" acceptance case.
     */
    @SuppressWarnings("unchecked")
    static void testSkewDivergence(TestFramework t) {
        String json = """
                {"seed":42,"maxIntermediateRows":3000000,
                 "tables":[
                   {"name":"A","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3}]},
                   {"name":"B","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3},
                                                       {"distribution":"zipf","ndv":100,"skew":1.3,"offset":100000}]},
                   {"name":"C","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3}]}],
                 "edges":[
                   {"left":"A","right":"B","leftColumn":0,"rightColumn":0,"selectivity":0.001},
                   {"left":"B","right":"C","leftColumn":1,"rightColumn":0,"selectivity":0.01}]}
                """;
        Map<String, Object> resp = TestSupport.simulate(json);
        Map<String, Object> cmp = (Map<String, Object>) resp.get("comparison");
        List<Map<String, Object>> irows =
                (List<Map<String, Object>>) resp.get("intermediateRows");

        double abEst = estimatedFor(irows, List.of("A", "B"));
        double abAct = actualFor(irows, List.of("A", "B"));
        double bcEst = estimatedFor(irows, List.of("B", "C"));
        double bcAct = actualFor(irows, List.of("B", "C"));

        // Estimator believes AB is the cheaper 2-way join...
        t.check(abEst < bcEst,
                "estimator ranks A⋈B cheaper than B⋈C (" + (long) abEst + " vs " + (long) bcEst + ")");
        // ...but reality is exactly reversed (correlated skew inflates AB; shifted
        // domains empty BC).
        t.check(abAct > abEst * 5,
                "correlated hot keys make real A⋈B far exceed estimate (" + abAct + " vs "
                        + (long) abEst + ")");
        t.check(bcAct < bcEst / 100,
                "disjoint value domains make real B⋈C far below estimate (" + bcAct + " vs "
                        + (long) bcEst + ")");

        t.eq(cmp.get("plansAgree"), false,
                "estimated and true optimal plans DIVERGE under skew/stale stats");
        double regret = ((Number) cmp.get("relativeRegret")).doubleValue();
        t.check(regret > 0.1,
                "following the estimated plan costs >10% extra in reality (regret="
                        + fmt(regret) + ")");

        // Estimated plan is a left-deep A-first chain; the true-optimal plan may be
        // left-deep C-first or bushy. Either way its left-deep linearization (if any) must
        // not be the estimator's A,B,C order.
        Map<String, Object> estOpt = (Map<String, Object>) resp.get("estimatedOptimal");
        Map<String, Object> actOpt = (Map<String, Object>) resp.get("actualOptimal");
        List<String> estOrder = (List<String>) estOpt.get("leftDeepOrder");
        List<String> actOrder = (List<String>) actOpt.get("leftDeepOrder");
        t.eq(estOrder, List.of("A", "B", "C"), "estimator picks the A,B,C left-deep chain");
        t.check(actOrder == null || !actOrder.equals(estOrder),
                "true optimal plan is not the estimator's A,B,C chain (actual order="
                        + actOrder + ")");
    }

    /** Fixed skew scenario: correlated keys inflate, fully disjoint domains empty BC. */
    @SuppressWarnings("unchecked")
    static void testExplicitSkewScenario(TestFramework t) {
        String json = """
                {"seed":99,"maxIntermediateRows":2000000,
                 "tables":[
                   {"name":"A","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3}]},
                   {"name":"B","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3},
                                                       {"distribution":"zipf","ndv":100,"skew":1.3,"offset":1000}]},
                   {"name":"C","rows":8000,"columns":[{"distribution":"zipf","ndv":100,"skew":1.3}]}],
                 "edges":[
                   {"left":"A","right":"B","leftColumn":0,"rightColumn":0},
                   {"left":"B","right":"C","leftColumn":1,"rightColumn":0}]}
                """;
        Map<String, Object> resp = TestSupport.simulate(json);
        List<Map<String, Object>> rows =
                (List<Map<String, Object>>) resp.get("intermediateRows");
        // A⋈B correlated: actual above estimate → ratio below 1.
        Double abRatio = ratioFor(rows, List.of("A", "B"));
        t.check(abRatio != null && abRatio < 0.9,
                "correlated hot keys inflate A⋈B (est/actual=" + fmt(abRatio) + ")");
        // B⋈C disjoint domains: exactly zero matches.
        double bcAct = actualFor(rows, List.of("B", "C"));
        double bcEst = estimatedFor(rows, List.of("B", "C"));
        t.eq(bcAct, 0.0, "fully shifted value domains make real B⋈C empty");
        t.check(bcEst > 0, "estimator still predicts a non-empty B⋈C (" + (long) bcEst + ")");
    }

    @SuppressWarnings("unchecked")
    static void testCapping(TestFramework t) {
        // Tiny cap: 3-table chain with a big first join must abort and flag capped subsets.
        String json = """
                {"seed":1,"maxIntermediateRows":1000,
                 "tables":[
                   {"name":"A","rows":20000,"columns":[{"distribution":"uniform","ndv":10}]},
                   {"name":"B","rows":20000,"columns":[{"distribution":"uniform","ndv":10}]},
                   {"name":"C","rows":200,"columns":[{"distribution":"uniform","ndv":10}]}],
                 "edges":[
                   {"left":"A","right":"B","leftColumn":0,"rightColumn":0},
                   {"left":"B","right":"C","leftColumn":0,"rightColumn":0}]}
                """;
        Map<String, Object> resp = TestSupport.simulate(json);
        Map<String, Object> actual = (Map<String, Object>) resp.get("actualOptimal");
        t.eq(actual.get("cappedSubsets"), true, "row cap surfaced in response");
    }

    @SuppressWarnings("unchecked")
    private static Double ratioFor(List<Map<String, Object>> rows, List<String> tables) {
        for (Map<String, Object> row : rows) {
            List<String> names = (List<String>) row.get("tables");
            if (names.equals(tables)) {
                Object ratio = row.get("ratioEstimatedOverActual");
                return ratio == null ? null : ((Number) ratio).doubleValue();
            }
        }
        throw new AssertionError("subset " + tables + " missing from simulation response");
    }

    @SuppressWarnings("unchecked")
    private static double estimatedFor(List<Map<String, Object>> rows, List<String> tables) {
        return metricFor(rows, tables, "estimatedRows");
    }

    @SuppressWarnings("unchecked")
    private static double actualFor(List<Map<String, Object>> rows, List<String> tables) {
        return metricFor(rows, tables, "actualRows");
    }

    @SuppressWarnings("unchecked")
    private static double metricFor(List<Map<String, Object>> rows, List<String> tables, String key) {
        for (Map<String, Object> row : rows) {
            List<String> names = (List<String>) row.get("tables");
            if (names.equals(tables)) {
                return ((Number) row.get(key)).doubleValue();
            }
        }
        throw new AssertionError("subset " + tables + " missing from simulation response");
    }

    private static String fmt(Double d) {
        return d == null ? "null" : String.format("%.3f", d);
    }
}
