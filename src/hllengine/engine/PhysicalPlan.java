package hllengine.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Physical plan derived from the logical plan.
 *
 * <p>This engine has a single execution strategy per logical node, so the
 * physical tree mirrors the logical tree one-to-one. It is kept as a separate
 * object deliberately: {@code explain} shows both trees, and costs are
 * attached here. Costs are simple cardinality-based estimates used purely to
 * make the plan tangible &mdash; they are not guarantees about runtime.
 */
public final class PhysicalPlan {

    private final List<Map<String, Object>> operators;
    private final double estimatedTotalCost;

    private PhysicalPlan(List<Map<String, Object>> operators, double estimatedTotalCost) {
        this.operators = operators;
        this.estimatedTotalCost = estimatedTotalCost;
    }

    public static PhysicalPlan from(LogicalPlan logical) {
        List<Map<String, Object>> ops = new ArrayList<>();
        double total = 0;
        double rows = -1; // unknown until runtime; Scan uses symbolic N
        for (LogicalPlan.Node node : logical.nodes()) {
            Map<String, Object> op = new LinkedHashMap<>();
            double cost;
            switch (node) {
                case LogicalPlan.Scan scan -> {
                    op.put("operator", "TableScan");
                    op.put("dataset", scan.dataset);
                    op.put("estimatedRows", "N");
                    cost = 1.0;
                }
                case LogicalPlan.Filter f -> {
                    op.put("operator", "Filter");
                    op.put("predicate", f.source);
                    op.put("selectivityAssumption", 0.5);
                    cost = 1.0;
                }
                case LogicalPlan.Project p -> {
                    op.put("operator", "Project");
                    op.put("fields", p.fields);
                    cost = 0.2;
                }
                case LogicalPlan.Aggregate a -> {
                    op.put("operator", "HashAggregate");
                    op.put("groupBy", a.groupBy);
                    List<Object> fnNames = new ArrayList<>();
                    for (AggSpec s : a.aggregates) fnNames.add(s.fn() + "(" + s.field() + ")");
                    op.put("aggregates", fnNames);
                    cost = 2.0;
                }
                case LogicalPlan.Limit l -> {
                    op.put("operator", "Limit");
                    op.put("limit", l.limit);
                    cost = 0.0;
                }
                default -> throw new IllegalStateException("unknown plan node: " + node);
            }
            total += cost;
            ops.add(op);
        }
        return new PhysicalPlan(ops, total);
    }

    public Map<String, Object> describe() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("physicalPlan", operators);
        Map<String, Object> cost = new LinkedHashMap<>();
        cost.put("model", "unit weights per operator (1.0 scan/filter, 2.0 aggregate, 0.2 project)");
        cost.put("estimatedTotalCost", estimatedTotalCost);
        cost.put("note", "planning hint only, not a measured runtime");
        out.put("cost", cost);
        return out;
    }
}
