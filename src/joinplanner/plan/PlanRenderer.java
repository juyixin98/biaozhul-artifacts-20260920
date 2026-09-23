package joinplanner.plan;

import joinplanner.json.J;
import joinplanner.model.Edge;
import joinplanner.model.Spec;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Renders a solved plan (and its estimation provenance) into the response Object graph. */
public final class PlanRenderer {

    private PlanRenderer() {}

    public static Map<String, Object> renderPlanTree(PlanNode node, Spec spec) {
        Map<String, Object> out = J.newObj();
        out.put("outputRows", roundNumber(node.outputRows()));
        if (node.leaf()) {
            out.put("type", "scan");
            out.put("table", node.table());
            out.put("estimatedRows", roundNumber(node.outputRows()));
            return out;
        }
        out.put("type", "join");
        out.put("nodeCost", roundNumber(node.nodeCost()));
        out.put("on", node.joinEdges());
        out.put("left", renderPlanTree(node.left(), spec));
        out.put("right", renderPlanTree(node.right(), spec));
        out.put("expression", linearExpression(node));
        out.put("tables", namesIn(node.mask(), spec));
        return out;
    }

    /** Flattened post-order list of join operations, numbered 0..k-1. */
    public static List<Map<String, Object>> renderSteps(PlanNode node, Spec spec) {
        List<Map<String, Object>> steps = new ArrayList<>();
        flatten(node, spec, steps, new java.util.IdentityHashMap<>());
        return steps;
    }

    private static void flatten(PlanNode node, Spec spec, List<Map<String, Object>> steps,
                                java.util.IdentityHashMap<PlanNode, Integer> stepId) {
        if (node.leaf()) {
            return;
        }
        flatten(node.left(), spec, steps, stepId);
        flatten(node.right(), spec, steps, stepId);
        int id = steps.size();
        stepId.put(node, id);
        Map<String, Object> step = J.newObj();
        step.put("step", id);
        step.put("operation", "inner join");
        step.put("left", describeRef(node.left(), stepId));
        step.put("right", describeRef(node.right(), stepId));
        step.put("on", node.joinEdges());
        step.put("estimatedOutputRows", roundNumber(node.outputRows()));
        step.put("nodeCost", roundNumber(node.nodeCost()));
        step.put("expression", linearExpression(node));
        steps.add(step);
    }

    private static Map<String, Object> describeRef(PlanNode node,
                                                   java.util.IdentityHashMap<PlanNode, Integer> stepId) {
        Map<String, Object> ref = J.newObj();
        if (node.leaf()) {
            ref.put("kind", "scan");
            ref.put("table", node.table());
            ref.put("estimatedRows", roundNumber(node.outputRows()));
        } else {
            ref.put("kind", "intermediate");
            ref.put("step", stepId.get(node));
            ref.put("estimatedRows", roundNumber(node.outputRows()));
        }
        return ref;
    }

    /** Fully parenthesized linear rendering, e.g. ((A ⋈ B) ⋈ C). */
    public static String linearExpression(PlanNode node) {
        if (node.leaf()) {
            return node.table();
        }
        return "(" + linearExpression(node.left()) + " ⋈ " + linearExpression(node.right()) + ")";
    }

    /** Left-deep join sequence of table names if the tree is left-deep, else null. */
    public static List<String> leftDeepOrder(PlanNode node) {
        List<String> order = new ArrayList<>();
        PlanNode cur = node;
        while (!cur.leaf()) {
            if (!cur.right().leaf()) {
                return null;
            }
            order.add(0, cur.right().table());
            cur = cur.left();
        }
        order.add(0, cur.table());
        return order;
    }

    public static List<String> namesIn(int mask, Spec spec) {
        List<String> names = new ArrayList<>();
        for (int i = 0; i < spec.n(); i++) {
            if ((mask & (1 << i)) != 0) {
                names.add(spec.tables.get(i).name());
            }
        }
        return names;
    }

    public static List<Map<String, Object>> edgeProvenance(Spec spec) {
        List<Map<String, Object>> edges = new ArrayList<>();
        for (Edge e : spec.edges) {
            Map<String, Object> m = J.newObj();
            m.put("left", e.left);
            m.put("right", e.right);
            m.put("selectivity", roundNumber(e.derivedSelectivity));
            m.put("derivedFrom", e.selectivitySource);
            if (e.on != null) {
                m.put("on", e.on);
            }
            edges.add(m);
        }
        return edges;
    }

    public static List<Map<String, Object>> subsetCardinalities(Spec spec, Estimator est) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int mask = 1; mask <= est.fullMask(); mask++) {
            if (!est.connected(mask)) {
                continue;
            }
            Map<String, Object> row = J.newObj();
            row.put("tables", namesIn(mask, spec));
            row.put("estimatedRows", roundNumber(est.card(mask)));
            if (est.saturated(mask)) {
                row.put("saturated", true);
            }
            rows.add(row);
        }
        return rows;
    }

    /** Render doubles without scientific noise; keep tiny values precise. */
    public static Object roundNumber(double d) {
        if (d == 0) {
            return 0;
        }
        double a = Math.abs(d);
        if (a >= 1 || a == 0) {
            if (a >= 1e12) {
                return d; // keep double; writer emits exponent form
            }
            return Math.rint(d * 1000) / 1000.0;
        }
        if (a >= 1e-6) {
            return Math.rint(d * 1e9) / 1e9;
        }
        return d;
    }
}
