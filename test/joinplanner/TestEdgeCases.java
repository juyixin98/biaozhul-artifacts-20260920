package joinplanner;

import joinplanner.json.BadInputException;
import joinplanner.model.Spec;
import joinplanner.plan.Estimator;
import joinplanner.plan.PlanService;
import joinplanner.plan.PlanService.DisconnectedException;

import java.util.List;
import java.util.Map;

public class TestEdgeCases {

    public static void main(String[] args) {
        TestFramework t = new TestFramework();

        // ---- validation errors (HTTP 400 at the API layer) ----
        t.throwsContaining("At most 8", () -> TestSupport.plan(nineTables()));
        t.throwsContaining("'tables' is required", () -> TestSupport.plan("{}"));
        t.throwsContaining("Duplicate table", () -> TestSupport.plan("""
                {"tables":[{"name":"X","rows":1},{"name":"X","rows":2}],"edges":[]}"""));
        t.throwsContaining("unknown table", () -> TestSupport.plan("""
                {"tables":[{"name":"A","rows":1},{"name":"B","rows":2}],
                 "edges":[{"left":"A","right":"Z"}]}"""));
        t.throwsContaining("self-join", () -> TestSupport.plan("""
                {"tables":[{"name":"A","rows":1}],"edges":[{"left":"A","right":"A"}]}"""));
        t.throwsContaining("selectivity must be within", () -> TestSupport.plan("""
                {"tables":[{"name":"A","rows":1},{"name":"B","rows":2}],
                 "edges":[{"left":"A","right":"B","selectivity":2}]}"""));
        t.throwsContaining("rows is required", () -> TestSupport.plan("""
                {"tables":[{"name":"A"}],"edges":[]}"""));
        t.throwsContaining("costModel", () -> TestSupport.plan("""
                {"tables":[{"name":"A","rows":1},{"name":"B","rows":2}],
                 "edges":[{"left":"A","right":"B","selectivity":0.5}],
                 "options":{"costModel":"MADE_UP"}}"""));
        t.throwsContaining("must be a JSON object", () ->
                joinplanner.json.J.obj("not-an-object", "body"));

        // ---- disconnected graph (HTTP 422 at the API layer) ----
        String disconnected = """
                {"tables":[{"name":"A","rows":100},{"name":"B","rows":200},
                           {"name":"C","rows":300},{"name":"D","rows":400}],
                 "edges":[{"left":"A","right":"B","selectivity":0.1},
                          {"left":"C","right":"D","selectivity":0.2}]}
                """;
        try {
            TestSupport.plan(disconnected);
            t.check(false, "disconnected graph must throw DisconnectedException");
        } catch (DisconnectedException e) {
            t.eq(e.body.get("connected"), false, "flagged disconnected");
            t.eq(e.body.get("error"), "DISCONNECTED_GRAPH", "error code present");
            t.eq(((Number) e.body.get("componentCount")).intValue(), 2, "two components");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> comps = (List<Map<String, Object>>) e.body.get("components");
            t.eq(comps.size(), 2, "two component plans");
            @SuppressWarnings("unchecked")
            Map<String, Object> cart = (Map<String, Object>) e.body.get("ifForcedCartesian");
            // Component joins: AB = 100*200*.1 = 2000; CD = 300*400*.2 = 24000.
            // Forced Cartesian across components: (100*200) x (300*400) = 2,400,000,000.
            t.approx(((Number) cart.get("crossProductRows")).doubleValue(), 2_400_000_000.0, 1e-9,
                    "Cartesian cross-product of raw component rows made explicit");
        }

        // Three singleton components.
        String three = """
                {"tables":[{"name":"A","rows":10},{"name":"B","rows":20},{"name":"C","rows":30}],"edges":[]}
                """;
        try {
            TestSupport.plan(three);
            t.check(false, "three singletons disconnected");
        } catch (DisconnectedException e) {
            t.eq(((Number) e.body.get("componentCount")).intValue(), 3,
                    "three singleton components");
        }

        // ---- cardinality overflow / saturation ----
        String overflow = """
                {"tables":[{"name":"A","rows":1.0e12},{"name":"B","rows":1.0e12},
                           {"name":"C","rows":1.0e12}],
                 "edges":[{"left":"A","right":"B","selectivity":0.5},
                          {"left":"B","right":"C","selectivity":0.5}]}
                """;
        Map<String, Object> resp = TestSupport.plan(overflow);
        @SuppressWarnings("unchecked")
        Map<String, Object> opt = (Map<String, Object>) resp.get("optimalPlan");
        t.eq(opt.get("costOverflow"), true, "plan flagged for cost overflow");
        t.approx(((Number) opt.get("finalRows")).doubleValue(), 1e15, 0,
                "cardinality saturated at estimateCap");

        // Custom cap.
        String cap = """
                {"tables":[{"name":"A","rows":1000},{"name":"B","rows":1000}],
                 "edges":[{"left":"A","right":"B","selectivity":0.5}],
                 "options":{"estimateCap":100}}
                """;
        Map<String, Object> resp2 = TestSupport.plan(cap);
        @SuppressWarnings("unchecked")
        Map<String, Object> opt2 = (Map<String, Object>) resp2.get("optimalPlan");
        t.approx(((Number) opt2.get("finalRows")).doubleValue(), 100, 0, "custom estimate cap applied");
        t.eq(opt2.get("costOverflow"), true, "capping flagged as overflow");

        // Huge base table rows alone saturate (1e18 > default cap 1e15).
        String huge = """
                {"tables":[{"name":"A","rows":1.0e17}],"edges":[]}
                """;
        Map<String, Object> resp3 = TestSupport.plan(huge);
        @SuppressWarnings("unchecked")
        Map<String, Object> opt3 = (Map<String, Object>) resp3.get("optimalPlan");
        t.approx(((Number) opt3.get("finalRows")).doubleValue(), 1e15, 0, "single huge table capped");

        // No NaN poisoning: saturated plans still compare and return a deterministic winner.
        Spec spec = TestSupport.spec(overflow);
        Estimator est = new Estimator(spec);
        for (int mask = 1; mask <= est.fullMask(); mask++) {
            double c = est.card(mask);
            t.check(!Double.isNaN(c) && !Double.isInfinite(c),
                    "no NaN/Infinity cardinalities (mask " + Integer.toBinaryString(mask) + ")");
        }

        // ---- single table plan (no edges) is legal ----
        Map<String, Object> one = TestSupport.plan(
                "{\"tables\":[{\"name\":\"A\",\"rows\":42}],\"edges\":[]}");
        @SuppressWarnings("unchecked")
        Map<String, Object> oneOpt = (Map<String, Object>) one.get("optimalPlan");
        t.approx(((Number) oneOpt.get("finalRows")).doubleValue(), 42, 0, "single table scan");
        t.approx(((Number) oneOpt.get("totalCost")).doubleValue(), 0, 0, "single table zero join cost");

        if (t.failures.isEmpty()) {
            System.out.println("PASS TestEdgeCases (" + t.checks() + " checks)");
        } else {
            System.out.println("FAIL TestEdgeCases");
            t.failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }

    private static String nineTables() {
        StringBuilder sb = new StringBuilder("{\"tables\":[");
        for (int i = 0; i < 9; i++) {
            if (i > 0) sb.append(',');
            sb.append("{\"name\":\"T").append(i).append("\",\"rows\":100}");
        }
        sb.append("],\"edges\":[");
        for (int i = 1; i < 9; i++) {
            if (i > 1) sb.append(',');
            sb.append("{\"left\":\"T").append(i - 1).append("\",\"right\":\"T").append(i)
                    .append("\",\"selectivity\":0.1}");
        }
        sb.append("]}");
        return sb.toString();
    }

    private static String tables(int n) {
        StringBuilder sb = new StringBuilder("{\"tables\":[");
        for (int i = 0; i < n; i++) {
            if (i > 0) sb.append(',');
            sb.append("{\"name\":\"T").append(i).append("\",\"rows\":100}");
        }
        sb.append(']');
        return sb.toString();
    }

    private static String edgesChain(int n) {
        StringBuilder sb = new StringBuilder(",\"edges\":[");
        for (int i = 1; i < n; i++) {
            if (i > 1) sb.append(',');
            sb.append("{\"left\":\"T").append(i - 1).append("\",\"right\":\"T").append(i)
                    .append("\",\"selectivity\":0.1}");
        }
        sb.append("]}");
        return sb.toString();
    }
}
