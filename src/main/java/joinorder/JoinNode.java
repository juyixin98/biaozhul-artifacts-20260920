package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 连接节点：等值连接（HashJoin）或笛卡尔积（NestedLoop，无连接谓词）。
 * edges 为本节点真正使用的连接边（空 => 笛卡尔积）。
 */
public final class JoinNode extends PlanNode {
    public final PlanNode left;
    public final PlanNode right;
    /** 本连接使用的边（索引列表，与 Model.edges 对应）。 */
    public final List<Integer> edgeIds;

    public JoinNode(PlanNode left, PlanNode right, List<Integer> edgeIds) {
        super(left.mask | right.mask);
        this.left = left;
        this.right = right;
        this.edgeIds = edgeIds;
        this.cartesian = edgeIds.isEmpty();
    }

    public List<Edge> edges(Model model) {
        List<Edge> r = new ArrayList<>();
        for (int id : edgeIds) r.add(model.edges.get(id));
        return r;
    }

    @Override public String nodeType() { return cartesian ? "cartesian-join" : "hash-join"; }

    @Override public Map<String, Object> toMap(Model model) {
        Map<String, Object> m = baseMap(model);
        List<String> preds = new ArrayList<>();
        for (Edge e : edges(model)) {
            for (EqPredicate p : e.predicates) preds.add(p.toString());
        }
        m.put("predicates", preds);
        m.put("left", left.toMap(model));
        m.put("right", right.toMap(model));
        return m;
    }

    @Override String label(Model model) {
        StringBuilder sb = new StringBuilder();
        if (cartesian) {
            sb.append("CartesianJoin");
        } else {
            sb.append("HashJoin ");
            List<String> ps = new ArrayList<>();
            for (Edge e : edges(model)) {
                for (EqPredicate p : e.predicates) ps.add(p.toString());
            }
            sb.append(ps);
        }
        sb.append("  [est rows=").append(round(estimated.rowCount))
          .append(", cost=").append(round(estimatedCost));
        if (actual != null) {
            sb.append(" | actual rows=").append((long) actual.rowCount)
              .append(", cost=").append(round(actualCost));
        }
        sb.append(']');
        return sb.toString();
    }

    @Override List<PlanNode> children() {
        List<PlanNode> r = new ArrayList<>();
        r.add(left);
        r.add(right);
        return r;
    }
}
