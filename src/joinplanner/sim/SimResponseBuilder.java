package joinplanner.sim;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import joinplanner.core.Stats;
import joinplanner.core.ValidatedProblem;

/** Builds the JSON body for /api/simulate. */
public final class SimResponseBuilder {

    public Map<String, Object> build(Simulator.SimResult result) {
        Map<String, Object> resp = new LinkedHashMap<>();
        ValidatedProblem p = result.problem();

        resp.put("summary", summary(result));
        resp.put("warnings", new ArrayList<>(p.warnings()));
        resp.put("estimatedPlan", estimatedPlan(result));
        resp.put("reality", reality(result));
        resp.put("verdict", verdict(result));
        resp.put("subsetCardinalities", subsetCards(result));
        resp.put("orderComparison", orderComparison(result));
        return resp;
    }

    private Map<String, Object> summary(Simulator.SimResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tableCount", r.problem().n());
        m.put("seed", r.seed());
        m.put("estimatedBestOrder", r.estimatedJoinOrder());
        m.put("estimatedBestCost", finite(r.estimatedPlanCost()));
        m.put("trueBestOrder", r.trueBestOrder());
        m.put("trueBestCost", r.trueBestCost());
        m.put("estimatedAndTrueBestAgree", r.ordersAgree());
        return m;
    }

    private Map<String, Object> estimatedPlan(Simulator.SimResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("dpJoinOrder", r.estimatedPlan().joinOrderNames());
        m.put("dpTotalCost", finite(r.estimatedPlan().totalCost()));
        m.put("note", "DP output is bushy; dpJoinOrder lists tree leaves. "
                + "estimatedBestOrder is the best left-deep order under the model, "
                + "which is directly comparable to trueBestOrder.");
        return m;
    }

    private Map<String, Object> reality(Simulator.SimResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("method", "synthetic Zipf-skewed data; exact hash-join execution with "
                + "multiset aggregation over all referenced join columns");
        m.put("trueCostModel", "sum of exact intermediate row counts over left-deep order");
        return m;
    }

    private Map<String, Object> verdict(Simulator.SimResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (r.ordersAgree()) {
            m.put("outcome", "AGREE");
            m.put("message", "Estimated optimum equals true optimum on this data.");
        } else {
            m.put("outcome", "DIVERGE");
            long trueCostOfEst = r.orders().stream()
                    .filter(o -> o.order().equals(r.estimatedJoinOrder()))
                    .mapToLong(Simulator.OrderComparison::trueCost)
                    .findFirst().orElse(Long.MIN_VALUE);
            double regret = r.trueBestCost() == 0 ? 0.0
                    : (double) trueCostOfEst / r.trueBestCost();
            m.put("message", "Estimator prefers " + r.estimatedJoinOrder()
                    + " but measured execution says " + r.trueBestOrder()
                    + " is cheaper. Skew violates the uniform/independent-value "
                    + "assumption behind 1/NDV selectivity.");
            m.put("trueCostOfEstimatedBestOrder", trueCostOfEst);
            m.put("trueCostOfTrueBestOrder", r.trueBestCost());
            m.put("regretRatio", regret);
        }
        return m;
    }

    private List<Map<String, Object>> subsetCards(Simulator.SimResult r) {
        List<Map<String, Object>> out = new ArrayList<>();
        r.subsetCards().forEach((name, pair) -> {
            long est = pair[0];
            long actual = pair[1];
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("subset", name);
            m.put("estimatedRows", est);
            m.put("actualRows", actual);
            m.put("estimateOverActual", actual == 0
                    ? (est == 0 ? 1.0 : null)
                    : round((double) est / actual));
            out.add(m);
        });
        return out;
    }

    private List<Map<String, Object>> orderComparison(Simulator.SimResult r) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (var o : r.orders()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("order", o.order());
            m.put("estimatedCost", finite(o.estimatedCost()));
            m.put("trueCost", o.trueCost());
            m.put("estimatedRank", o.estRanking());
            m.put("trueRank", o.trueRanking());
            out.add(m);
        }
        return out;
    }

    private Object finite(double d) {
        return Double.isFinite(d) ? d : null;
    }

    private double round(double d) {
        return Math.rint(d * 1e6) / 1e6;
    }

    public Map<String, Object> error(String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", code);
        m.put("message", message);
        return m;
    }

    static Object overflowHint(long limit) {
        return "Raise maxRows, reduce table sizes, or reduce zipf skew. "
                + "Estimator-side cost remains available via /api/plan.";
    }
}
