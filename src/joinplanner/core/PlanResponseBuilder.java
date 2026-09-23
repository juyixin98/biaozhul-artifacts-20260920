package joinplanner.core;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import joinplanner.model.JoinType;
import joinplanner.model.PlanNode;
import joinplanner.model.TableSpec;

/**
 * Assembles the explainable JSON response for a planning request: resolved
 * statistics with derivations, the chosen plan tree with per-node estimates,
 * and the exhaustive enumeration of legal left-deep orders.
 */
public final class PlanResponseBuilder {

    private final ValidatedProblem problem;

    public PlanResponseBuilder(ValidatedProblem problem) {
        this.problem = problem;
    }

    public Map<String, Object> build(DpResult dp, List<OrderEnumerator.LeftDeepOrder> orders) {
        Map<String, Object> resp = new LinkedHashMap<>();

        resp.put("summary", summary(dp));
        resp.put("warnings", new ArrayList<>(problem.warnings()));
        resp.put("tables", tablesJson());
        resp.put("edges", edgesJson());
        resp.put("plan", planTreeJson(dp.root()));
        resp.put("joinOrder", dp.joinOrderNames());
        resp.put("enumeration", enumerationJson(orders));
        return resp;
    }

    private Map<String, Object> summary(DpResult dp) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tableCount", problem.n());
        m.put("edgeCount", problem.resolvedEdges().size());
        m.put("totalCost", finiteOrNull(dp.totalCost()));
        m.put("costOverflow", dp.costOverflow());
        m.put("costModel", "sum of estimated intermediate-result row counts "
                + "(base scans cost 0)");
        m.put("cardinalityModel", "product of base cardinalities times product of "
                + "edge selectivities (selectivity assumed constant per edge)");
        return m;
    }

    private List<Map<String, Object>> tablesJson() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (TableSpec t : problem.spec().tables()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", t.name());
            m.put("rows", t.rows());
            m.put("unique", t.unique());
            if (t.alias() != null) {
                m.put("alias", t.alias());
            }
            out.add(m);
        }
        return out;
    }

    private List<Map<String, Object>> edgesJson() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (var e : problem.resolvedEdges()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("left", e.spec().leftTable() + "." + e.spec().leftColumn());
            m.put("right", e.spec().rightTable() + "." + e.spec().rightColumn());
            m.put("selectivity", e.selectivity());
            m.put("selectivitySource", e.source().name());
            m.put("derivation", e.explanation());
            out.add(m);
        }
        return out;
    }

    private Map<String, Object> planTreeJson(PlanNode node) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (node.isLeaf()) {
            TableSpec t = problem.spec().tables().get(node.tableIndex());
            m.put("type", "SCAN");
            m.put("table", t.name());
            m.put("estimatedRows", finiteOrNull(node.outputRows()));
            m.put("nodeCost", 0.0);
        } else {
            var edge = node.edgeIndex() >= 0
                    ? problem.resolvedEdges().get(node.edgeIndex()) : null;
            m.put("type", node.joinType() == JoinType.CROSS ? "CROSS_JOIN" : "INNER_JOIN");
            m.put("estimatedRows", finiteOrNull(node.outputRows()));
            m.put("nodeCost", finiteOrNull(node.nodeCost()));
            m.put("rowsOverflow", Stats.overflowed(node.outputRows()));
            if (edge != null) {
                Map<String, Object> pred = new LinkedHashMap<>();
                pred.put("left", edge.spec().leftTable() + "." + edge.spec().leftColumn());
                pred.put("right", edge.spec().rightTable() + "." + edge.spec().rightColumn());
                pred.put("selectivity", edge.selectivity());
                pred.put("selectivitySource", edge.source().name());
                m.put("predicate", pred);
                m.put("explanation", "inner join on "
                        + edge.spec().leftTable() + "." + edge.spec().leftColumn() + " = "
                        + edge.spec().rightTable() + "." + edge.spec().rightColumn()
                        + " (s=" + trim(edge.selectivity()) + ")");
            } else {
                m.put("predicate", null);
                m.put("explanation", "cartesian product (join graph was disconnected)");
            }
            m.put("left", planTreeJson(node.left()));
            m.put("right", planTreeJson(node.right()));
        }
        return m;
    }

    private Map<String, Object> enumerationJson(List<OrderEnumerator.LeftDeepOrder> orders) {
        List<Map<String, Object>> legal = new ArrayList<>();
        long legalCount = 0;
        double best = Double.POSITIVE_INFINITY;
        for (OrderEnumerator.LeftDeepOrder o : orders) {
            if (!o.legal()) {
                continue;
            }
            legalCount++;
            best = Math.min(best, o.cost());
            if (legal.size() < 100) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("order", o.order());
                m.put("estimatedCost", finiteOrNull(o.cost()));
                legal.add(m);
            }
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("space", "left-deep permutations");
        m.put("permutationsConsidered", orders.size());
        m.put("legalOrders", legalCount);
        m.put("bestLeftDeepCost", Double.isInfinite(best) ? null : finiteOrNull(best));
        m.put("note", "DP searches the full bushy space; this list ranks left-deep "
                + "orders for verification. Costs are the estimator's.");
        m.put("orders", legal);
        return m;
    }

    /** JSON cannot encode Infinity; render null but keep overflow flags. */
    private Object finiteOrNull(double d) {
        return Double.isFinite(d) ? d : null;
    }

    private String trim(double d) {
        if (d >= 0.001) {
            return String.format(java.util.Locale.ROOT, "%.6g", d);
        }
        return Double.toString(d);
    }
}
