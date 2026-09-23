package joinplanner.plan;

import joinplanner.json.J;
import joinplanner.model.Spec;
import joinplanner.model.Table;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Orchestrates a {@code /plan} request: estimation, connectivity check, DP solve, and
 * response assembly. A disconnected join graph is reported explicitly with one best
 * plan per connected component — an inner join of disconnected components is a
 * Cartesian product and is never chosen silently.
 */
public final class PlanService {

    /** Thrown for a disconnected graph; the HTTP layer maps it to status 422. */
    public static final class DisconnectedException extends RuntimeException {
        public final transient Map<String, Object> body;

        public DisconnectedException(Map<String, Object> body) {
            super("Join graph is disconnected");
            this.body = body;
        }
    }

    public Map<String, Object> plan(Spec spec) {
        Estimator est = new Estimator(spec);
        List<Integer> components = GraphComponents.components(spec);

        if (components.size() > 1) {
            throw new DisconnectedException(disconnectedBody(spec, est, components));
        }

        int fullMask = est.fullMask();
        Planner planner = new Planner(spec, est);
        DpSolution best = planner.solve(fullMask);

        Map<String, Object> resp = J.newObj();
        resp.put("connected", true);
        resp.put("tableCount", spec.n());
        resp.put("searchSpace", spec.leftDeepOnly ? "left-deep" : "bushy");
        resp.put("costModel", spec.costModel.name());

        Map<String, Object> optimal = J.newObj();
        optimal.put("tree", PlanRenderer.renderPlanTree(best.tree(), spec));
        optimal.put("steps", PlanRenderer.renderSteps(best.tree(), spec));
        optimal.put("expression", PlanRenderer.linearExpression(best.tree()));
        List<String> order = PlanRenderer.leftDeepOrder(best.tree());
        optimal.put("leftDeepOrder", order);
        optimal.put("isLeftDeep", order != null);
        optimal.put("totalCost", PlanRenderer.roundNumber(best.cost()));
        optimal.put("finalRows", PlanRenderer.roundNumber(est.card(fullMask)));
        optimal.put("costOverflow", best.saturated());
        resp.put("optimalPlan", optimal);

        if (spec.leftDeepOnly) {
            Planner bushy = new Planner(
                    new Spec(spec.tables, spec.edges, false, spec.costModel,
                            spec.defaultSelectivity, spec.estimateCap), est);
            DpSolution bushyBest = bushy.solve(fullMask);
            Map<String, Object> alt = J.newObj();
            alt.put("totalCost", PlanRenderer.roundNumber(bushyBest.cost()));
            alt.put("expression", PlanRenderer.linearExpression(bushyBest.tree()));
            resp.put("bushyComparison", alt);
        }

        resp.put("edgeSelectivity", PlanRenderer.edgeProvenance(spec));
        resp.put("subsetCardinalities", PlanRenderer.subsetCardinalities(spec, est));
        resp.put("estimateCap", spec.estimateCap);
        if (!spec.warnings.isEmpty()) {
            resp.put("warnings", spec.warnings);
        }
        return resp;
    }

    private Map<String, Object> disconnectedBody(Spec spec, Estimator est, List<Integer> components) {
        Map<String, Object> body = J.newObj();
        body.put("connected", false);
        body.put("error", "DISCONNECTED_GRAPH");
        body.put("message", "The join graph has " + components.size()
                + " connected components. An inner join across components would be a Cartesian "
                + "product; connect them with edges or plan the components separately.");
        body.put("componentCount", components.size());

        List<Map<String, Object>> componentPlans = new ArrayList<>();
        double summedCost = 0;
        for (int compMask : components) {
            Planner p = new Planner(spec, est);
            DpSolution s = p.solve(compMask);
            Map<String, Object> c = J.newObj();
            c.put("tables", PlanRenderer.namesIn(compMask, spec));
            c.put("bestExpression", PlanRenderer.linearExpression(s.tree()));
            c.put("totalCost", PlanRenderer.roundNumber(s.cost()));
            c.put("finalRows", PlanRenderer.roundNumber(est.card(compMask)));
            componentPlans.add(c);
            summedCost += s.cost();
        }
        body.put("components", componentPlans);

        // The cheapest way to physically execute the disconnected query is the sum of the
        // component plans followed by Cartesian products. A Cartesian product sees only raw
        // rows — component-internal selectivities do not apply — so multiply base-table rows.
        double productRows = 1;
        boolean productSaturated = false;
        for (int compMask : components) {
            double baseRows = 1;
            for (int i = 0; i < spec.n(); i++) {
                if ((compMask & (1 << i)) != 0) {
                    baseRows *= spec.tables.get(i).rows();
                }
            }
            double next = productRows * baseRows;
            if (next >= spec.estimateCap || Double.isInfinite(next)) {
                productSaturated = true;
                next = spec.estimateCap;
            }
            productRows = next;
        }
        Map<String, Object> cartesian = J.newObj();
        cartesian.put("crossProductRows", PlanRenderer.roundNumber(productRows));
        cartesian.put("componentCostsSum", PlanRenderer.roundNumber(summedCost));
        if (productSaturated) {
            cartesian.put("saturated", true);
        }
        body.put("ifForcedCartesian", cartesian);
        if (!spec.warnings.isEmpty()) {
            body.put("warnings", spec.warnings);
        }
        return body;
    }
}
