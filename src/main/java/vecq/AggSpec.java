package vecq;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 聚合描述：count / sum / avg / min / max。
 *
 * count 支持 "*"（计数选择向量行数，重复行重复计数，NULL 不影响），
 * 也支持具体列（count(column) 忽略该列 NULL）。
 * sum/avg 仅适用于整型列；min/max 适用于整型列与字符串列；除 count(*) 外聚合列忽略 NULL。
 */
public record AggSpec(String func, String column, String alias) {

    public boolean isCountStar() {
        return func.equals("count") && (column.equals("*") || column.isEmpty());
    }

    public String outputName() {
        if (alias != null && !alias.isEmpty()) return alias;
        return func + "(" + column + ")";
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("func", func);
        m.put("column", column);
        if (alias != null) m.put("alias", alias);
        return m;
    }

    public static AggSpec fromJson(Object o) {
        if (o instanceof String s) {
            // 简写 "count(*)" / "sum(id)"
            int lp = s.indexOf('(');
            if (lp <= 0 || !s.endsWith(")")) {
                throw new InvalidQueryException("聚合简写形如 count(*) / sum(id)，实际: " + s);
            }
            return new AggSpec(s.substring(0, lp).trim(), s.substring(lp + 1, s.length() - 1).trim(), null);
        }
        Map<String, Object> m = Json.asMap(o);
        Object f = m.get("func");
        if (!(f instanceof String func) || func.isEmpty()) {
            throw new InvalidQueryException("聚合缺少 func 字段");
        }
        String col = m.getOrDefault("column", "*").toString();
        Object al = m.get("alias");
        return new AggSpec(func, col, al == null ? null : al.toString());
    }
}
