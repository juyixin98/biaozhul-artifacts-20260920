package joinplanner.plan;

import joinplanner.json.J;
import joinplanner.model.Spec;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Exhaustively lists every legal left-deep order and independently recomputes the
 * bushy optimum by recursion. Intended for small instances (the response itself
 * contains all permutations); used by the acceptance tests to prove DP optimality.
 */
public final class EnumerateService {

    public Map<String, Object> enumerate(Spec spec) {
        Estimator est = new Estimator(spec);
        List<Integer> components = GraphComponents.components(spec);
        if (components.size() > 1) {
            throw new PlanService.DisconnectedException(disconnected(spec, components));
        }

        BruteForce bf = new BruteForce(spec, est);
        List<BruteForce.OrderCost> orders = bf.enumerateLeftDeepOrders();

        List<Map<String, Object>> rows = new ArrayList<>();
        double minCost = Double.POSITIVE_INFINITY;
        for (BruteForce.OrderCost oc : orders) {
            Map<String, Object> row = J.newObj();
            row.put("order", oc.order());
            row.put("cost", PlanRenderer.roundNumber(oc.cost()));
            if (oc.saturated()) {
                row.put("saturated", true);
            }
            rows.add(row);
            minCost = Math.min(minCost, oc.cost());
        }

        double bushyMin = bf.bestBushyCost(est.fullMask());
        Planner planner = new Planner(spec, est);
        DpSolution dp = planner.solve(est.fullMask());

        Map<String, Object> resp = J.newObj();
        resp.put("tableCount", spec.n());
        resp.put("costModel", spec.costModel.name());
        resp.put("legalLeftDeepOrders", rows);
        resp.put("legalLeftDeepOrderCount", rows.size());

        Map<String, Object> verify = J.newObj();
        verify.put("dpCost", PlanRenderer.roundNumber(dp.cost()));
        verify.put("minEnumeratedLeftDeepCost", PlanRenderer.roundNumber(minCost));
        verify.put("bruteForceBushyCost", PlanRenderer.roundNumber(bushyMin));
        boolean match = closeEnough(dp.cost(), bushyMin);
        verify.put("dpMatchesBruteForce", match);
        if (!spec.leftDeepOnly) {
            verify.put("dpBeatsOrEqualsLeftDeep", dp.cost() <= minCost + 1e-9 * Math.max(1, minCost));
        }
        resp.put("verification", verify);
        resp.put("dpPlan", PlanRenderer.linearExpression(dp.tree()));
        if (!spec.warnings.isEmpty()) {
            resp.put("warnings", spec.warnings);
        }
        return resp;
    }

    private boolean closeEnough(double a, double b) {
        if (a == b) {
            return true;
        }
        double scale = Math.max(1, Math.max(Math.abs(a), Math.abs(b)));
        return Math.abs(a - b) <= 1e-9 * scale;
    }

    private Map<String, Object> disconnected(Spec spec, List<Integer> masks) {
        Map<String, Object> body = J.newObj();
        body.put("connected", false);
        body.put("error", "DISCONNECTED_GRAPH");
        List<List<String>> comps = new ArrayList<>();
        for (int m : masks) {
            comps.add(PlanRenderer.namesIn(m, spec));
        }
        body.put("components", comps);
        return body;
    }
}
