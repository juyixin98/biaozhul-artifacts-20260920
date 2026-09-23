package vecq;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 过滤表达式（执行计划 AST），sealed 层次：
 * <pre>
 *   Compare   col op literal            （int: = != &lt; &lt;= &gt; &gt;=；string: = !=）
 *   IsNull    col, negate               （IS NULL / IS NOT NULL，不产生 UNKNOWN）
 *   And / Or  多子表达式
 *   Not       单子表达式
 *   Union     顶层 OR 的“多结果集合并”形态：每个分支分别过滤，结果做多重集并集
 * </pre>
 *
 * 普通 Or 是标准 SQL 语义（每行至多保留一次，3VL）；Union 是本引擎显式提供的
 * “分支结果合并”语义（同一行可因多个分支命中而被重复选择）。
 * Union 只允许出现在过滤计划的根节点（由 {@link PlanBuilder} 校验）。
 */
public sealed interface FilterExpr
        permits FilterExpr.Compare, FilterExpr.IsNull, FilterExpr.And,
                FilterExpr.Or, FilterExpr.Not, FilterExpr.Union {

    Map<String, Object> toJson();

    record Compare(String column, String op, Object value) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("op", op);
            m.put("column", column);
            m.put("value", value);
            return m;
        }
    }

    record IsNull(String column, boolean negate) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("op", negate ? "isNotNull" : "isNull");
            m.put("column", column);
            return m;
        }
    }

    record And(List<FilterExpr> children) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            return logic("and", children);
        }
    }

    record Or(List<FilterExpr> children) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            return logic("or", children);
        }
    }

    record Not(FilterExpr child) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("op", "not");
            m.put("child", child.toJson());
            return m;
        }
    }

    /** 每个分支独立产出选择向量，再做多集合并（允许重复）。 */
    record Union(List<FilterExpr> branches) implements FilterExpr {
        @Override public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("op", "union");
            List<Object> bs = new ArrayList<>();
            for (FilterExpr b : branches) bs.add(b.toJson());
            m.put("branches", bs);
            return m;
        }
    }

    private static Map<String, Object> logic(String op, List<FilterExpr> kids) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("op", op);
        List<Object> cs = new ArrayList<>();
        for (FilterExpr c : kids) cs.add(c.toJson());
        m.put("children", cs);
        return m;
    }

    // ---------------- JSON 解析 ----------------

    static FilterExpr fromJson(Object o) {
        Map<String, Object> m = Json.asMap(o);
        Object opRaw = m.containsKey("op") ? m.get("op") : m.get("kind");
        String kind = opRaw == null ? "" : String.valueOf(opRaw);
        return switch (kind) {
            case "and" -> new And(children(m, "children"));
            case "or" -> new Or(children(m, "children"));
            case "not" -> {
                if (!m.containsKey("child")) throw new InvalidQueryException("not 表达式缺少 child 字段");
                yield new Not(FilterExpr.fromJson(m.get("child")));
            }
            case "union" -> new Union(children(m, "branches"));
            case "isnull", "is_null", "isNull" ->
                    new IsNull(reqCol(m), Boolean.TRUE.equals(m.get("negate")));
            case "notnull", "isnotnull", "isNotNull", "is_not_null" ->
                    new IsNull(reqCol(m), true);
            default -> {
                if (!m.containsKey("column")) {
                    throw new InvalidQueryException("无法识别的过滤算子 \"" + kind
                            + "\"，且缺少 column 字段");
                }
                String op = kind.isEmpty() ? "=" : kind;
                if (!m.containsKey("value")) {
                    throw new InvalidQueryException("比较表达式 " + m.get("column") + " " + op
                            + " 缺少 value 字段；IS NULL 请使用 {\"op\":\"isNull\",\"column\":...}");
                }
                yield new Compare(reqCol(m), op, m.get("value"));
            }
        };
    }

    private static List<FilterExpr> children(Map<String, Object> m, String key) {
        if (!m.containsKey(key)) throw new InvalidQueryException("逻辑算子缺少 " + key + " 字段");
        List<Object> list = Json.asList(m.get(key));
        if (list.isEmpty()) throw new InvalidQueryException("逻辑算子的 " + key + " 不能为空");
        List<FilterExpr> out = new ArrayList<>();
        for (Object c : list) out.add(FilterExpr.fromJson(c));
        return out;
    }

    private static String reqCol(Map<String, Object> m) {
        Object c = m.get("column");
        if (!(c instanceof String s) || s.isEmpty()) {
            throw new InvalidQueryException("过滤表达式缺少非空 column 字段");
        }
        return s;
    }
}
