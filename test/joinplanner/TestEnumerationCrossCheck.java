package joinplanner;

import joinplanner.model.CostModel;
import joinplanner.model.Spec;
import joinplanner.plan.BruteForce;
import joinplanner.plan.DpSolution;
import joinplanner.plan.Estimator;
import joinplanner.plan.PlanService;
import joinplanner.plan.Planner;

import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Acceptance test: for every small instance (n = 2..6) enumerate ALL legal left-deep
 * orders and independently recurse over all bushy trees, then assert the DP answer
 * matches the brute-force minimum. Runs over many randomized connected graphs.
 */
public class TestEnumerationCrossCheck {

    public static void main(String[] args) {
        TestFramework t = new Framework();
        Random rng = new Random(20260923L);

        for (CostModel model : CostModel.values()) {
            for (int n = 2; n <= 6; n++) {
                for (int trial = 0; trial < 60; trial++) {
                    String json = randomInstance(rng, n, model == CostModel.SUM_OF_INPUTS);
                    Spec spec = TestSupport.spec(json);
                    // Skip disconnected trials (enumeration endpoint rejects them; brute force
                    // over connected masks is still well-defined, but keep the semantics simple).
                    Estimator est = new Estimator(spec);
                    joinplanner.plan.GraphComponents gcs = null;
                    List<Integer> comps = joinplanner.plan.GraphComponents.components(spec);
                    if (comps.size() != 1) {
                        continue;
                    }
                    BruteForce bf = new BruteForce(spec, est);
                    Planner dp = new Planner(spec, est);
                    int full = (1 << n) - 1;
                    DpSolution dpSol = dp.solve(full);
                    double bushyMin = bf.bestBushyCost(full);

                    List<BruteForce.OrderCost> orders = bf.enumerateLeftDeepOrders();
                    double minLeftDeep = Double.POSITIVE_INFINITY;
                    for (BruteForce.OrderCost oc : orders) {
                        minLeftDeep = Math.min(minLeftDeep, oc.cost());
                    }

                    t.approx(dpSol.cost(), bushyMin, 1e-10,
                            "n=" + n + " trial=" + trial + " " + model + ": DP == brute-force bushy");
                    t.check(dpSol.cost() <= minLeftDeep + 1e-7 * Math.max(1, minLeftDeep),
                            "n=" + n + " " + model + ": bushy DP <= best left-deep");
                }
            }
        }

        // Deterministic exhaustive endpoint test: counts and exact minimum for a known n=3.
        String n3 = """
                {"tables":[{"name":"A","rows":100},{"name":"B","rows":1000},{"name":"C","rows":10000}],
                 "edges":[{"left":"A","right":"B","selectivity":0.1},
                          {"left":"B","right":"C","selectivity":0.01}]}
                """;
        Map<String, Object> resp = TestSupport.enumerate(n3);
        t.eq(((Number) resp.get("legalLeftDeepOrderCount")).intValue(), 4,
                "chain of 3 has 4 legal left-deep orders (A-B-C, C-B-A, B-A-C, B-C-A)");
        @SuppressWarnings("unchecked")
        Map<String, Object> verify = (Map<String, Object>) resp.get("verification");
        t.eq(verify.get("dpMatchesBruteForce"), true, "endpoint reports DP/brute-force match");

        // Complete graph K4: all 4! = 24 orders are legal.
        StringBuilder k4 = new StringBuilder(
                "{\"tables\":[{\"name\":\"A\",\"rows\":50},{\"name\":\"B\",\"rows\":80},"
                        + "{\"name\":\"C\",\"rows\":120},{\"name\":\"D\",\"rows\":200}],\"edges\":[");
        char[] names = {'A', 'B', 'C', 'D'};
        boolean first = true;
        for (int i = 0; i < 4; i++) {
            for (int j = i + 1; j < 4; j++) {
                if (!first) k4.append(',');
                first = false;
                double sel = 0.01 * (i + j + 1);
                k4.append("{\"left\":\"").append(names[i]).append("\",\"right\":\"")
                        .append(names[j]).append("\",\"selectivity\":").append(sel).append('}');
            }
        }
        k4.append("]}");
        Map<String, Object> resp4 = TestSupport.enumerate(k4.toString());
        t.eq(((Number) resp4.get("legalLeftDeepOrderCount")).intValue(), 24,
                "K4 admits all 24 permutations");
        @SuppressWarnings("unchecked")
        Map<String, Object> verify4 = (Map<String, Object>) resp4.get("verification");
        t.eq(verify4.get("dpMatchesBruteForce"), true, "K4 DP matches brute force");

        Framework ft = (Framework) t;
        if (ft.failures.isEmpty()) {
            System.out.println("PASS TestEnumerationCrossCheck (" + ft.checks()
                    + " checks across randomized n=2..6 instances)");
        } else {
            System.out.println("FAIL TestEnumerationCrossCheck");
            ft.failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }

    private static String randomInstance(Random rng, int n, boolean sumOfInputs) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"tables\":[");
        for (int i = 0; i < n; i++) {
            if (i > 0) sb.append(',');
            long rows = 1L + (long) (Math.pow(10, 1 + rng.nextInt(4)) * rng.nextDouble());
            sb.append("{\"name\":\"T").append(i).append("\",\"rows\":").append(rows).append('}');
        }
        sb.append("],\"edges\":[");
        // Build a random spanning tree (guarantees connectivity) then random extra edges.
        boolean[][] adj = new boolean[n][n];
        int[] order = shuffledOrder(rng, n);
        boolean first = true;
        for (int k = 1; k < n; k++) {
            int a = order[rng.nextInt(k)];
            int b = order[k];
            appendEdge(sb, first, a, b, rng);
            first = false;
            adj[a][b] = adj[b][a] = true;
        }
        int extra = rng.nextInt(n); // 0..n-1 extra edges
        for (int k = 0; k < extra; k++) {
            int a = rng.nextInt(n);
            int b = rng.nextInt(n);
            if (a == b || adj[a][b]) continue;
            appendEdge(sb, first, a, b, rng);
            first = false;
            adj[a][b] = adj[b][a] = true;
        }
        sb.append(']');
        if (sumOfInputs) {
            sb.append(",\"options\":{\"costModel\":\"SUM_OF_INPUTS\"}");
        }
        sb.append('}');
        return sb.toString();
    }

    private static void appendEdge(StringBuilder sb, boolean first, int a, int b, Random rng) {
        if (!first) sb.append(',');
        double sel = Math.pow(10, -rng.nextInt(4)) * (0.1 + rng.nextDouble());
        if (sel > 1) sel = 1;
        sb.append("{\"left\":\"T").append(a).append("\",\"right\":\"T").append(b)
                .append("\",\"selectivity\":").append(sel).append('}');
    }

    private static int[] shuffledOrder(Random rng, int n) {
        int[] order = new int[n];
        for (int i = 0; i < n; i++) {
            order[i] = i;
        }
        for (int i = n - 1; i > 0; i--) {
            int j = rng.nextInt(i + 1);
            int tmp = order[i];
            order[i] = order[j];
            order[j] = tmp;
        }
        return order;
    }

    /** Local alias so the failure list remains accessible at the bottom. */
    static final class Framework extends TestFramework {
    }
}
