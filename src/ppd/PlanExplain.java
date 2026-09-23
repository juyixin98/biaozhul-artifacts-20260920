package ppd;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 把计划树序列化为可读 JSON（用于导出执行计划）。 */
public final class PlanExplain {

    private PlanExplain() {}

    public static Map<String, Object> toJson(Plan p) {
        return switch (p) {
            case Plan.Scan s -> {
                Map<String, Object> m = base("Scan", s);
                m.put("table", s.table());
                yield m;
            }
            case Plan.Filter f -> {
                Map<String, Object> m = base("Filter", f);
                m.put("predicate", f.predicate().toSql());
                m.put("child", toJson(f.child()));
                yield m;
            }
            case Plan.Project pr -> {
                Map<String, Object> m = base("Project", pr);
                m.put("items", pr.items().stream().map(Expr::toSql).toList());
                m.put("child", toJson(pr.child()));
                yield m;
            }
            case Plan.InnerJoin ij -> {
                Map<String, Object> m = base("InnerJoin", ij);
                m.put("joinType", "inner");
                m.put("on", ij.onPredicates().stream().map(Expr::toSql).toList());
                m.put("left", toJson(ij.left()));
                m.put("right", toJson(ij.right()));
                yield m;
            }
            case Plan.LeftJoin lj -> {
                Map<String, Object> m = base("LeftJoin", lj);
                m.put("joinType", "left");
                m.put("on", lj.onPredicates().stream().map(Expr::toSql).toList());
                m.put("left", toJson(lj.left()));
                m.put("right", toJson(lj.right()));
                yield m;
            }
        };
    }

    private static Map<String, Object> base(String op, Plan p) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("op", op);
        m.put("outputColumns", describeSchema(p.schema()));
        return m;
    }

    /** 带 nullable 标记与列来源的 schema 描述。 */
    public static List<Map<String, Object>> describeSchema(Schema s) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Column c : s.columns()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", c.display());
            m.put("nullable", c.nullable());
            if (c.origin() != null) {
                Column r = c.rootOrigin();
                m.put("origin", r.display());
            }
            out.add(m);
        }
        return out;
    }

    /** 缩进文本形式。 */
    public static String toText(Plan p) {
        StringBuilder sb = new StringBuilder();
        print(sb, p, 0);
        return sb.toString();
    }

    private static void print(StringBuilder sb, Plan p, int depth) {
        sb.append("  ".repeat(depth));
        switch (p) {
            case Plan.Scan s -> sb.append("Scan(").append(s.table()).append(")\n");
            case Plan.Filter f -> {
                sb.append("Filter(").append(f.predicate().toSql()).append(")\n");
                print(sb, f.child(), depth + 1);
            }
            case Plan.Project pr -> {
                sb.append("Project(").append(pr.items().stream().map(Expr::toSql)
                        .reduce((a, b) -> a + ", " + b).orElse("")).append(")\n");
                print(sb, pr.child(), depth + 1);
            }
            case Plan.InnerJoin ij -> {
                sb.append("InnerJoin(on=").append(ij.onPredicates().stream().map(Expr::toSql)
                        .reduce((a, b) -> a + " AND " + b).orElse("")).append(")\n");
                print(sb, ij.left(), depth + 1);
                print(sb, ij.right(), depth + 1);
            }
            case Plan.LeftJoin lj -> {
                sb.append("LeftJoin(on=").append(lj.onPredicates().stream().map(Expr::toSql)
                        .reduce((a, b) -> a + " AND " + b).orElse("")).append(")\n");
                print(sb, lj.left(), depth + 1);
                print(sb, lj.right(), depth + 1);
            }
        }
    }
}
