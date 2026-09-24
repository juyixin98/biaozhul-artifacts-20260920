package bitserver;

import java.util.List;
import java.util.Map;

/**
 * 布尔表达式求值引擎（在索引位图上执行）。
 *
 * 支持的表达式（JSON 树）：
 *   {"op":"eq","col":"city","value":"BJ"}   列值精确匹配
 *   {"op":"in","col":"city","values":["BJ","SH"]}
 *   {"op":"and","args":[ ... ]}              与（空 args 视为全集）
 *   {"op":"or","args":[ ... ]}               或（空 args 视为空集）
 *   {"op":"not","arg":{ ... }}               非 —— 仅在当前存活行全集内取补：
 *                                            NOT x = alive AND (NOT x)
 *   {"op":"alive"}                            当前存活行全集（删除掩码的补集）
 *
 * NOT 不在“全数据集行空间”取补，而在存活行全集内取补，因此已删除行永远不会
 * 被 NOT “捞回来”。叶子谓词基于不可变索引，与存活掩码无关。
 */
public final class QueryEngine {

    private final BitmapIndex index;

    public QueryEngine(BitmapIndex index) {
        this.index = index;
    }

    /** 对叶子/复合表达式求值，返回未做存活裁剪的位图（NOT 内部已经遵守存活语义）。 */
    public Bitmap eval(Map<String, Object> expr, Bitmap alive) {
        Object opObj = expr.get("op");
        if (!(opObj instanceof String)) {
            throw new IllegalArgumentException("表达式缺少字符串类型的 \"op\" 字段: " + expr);
        }
        String op = (String) opObj;
        switch (op) {
            case "eq":
                return evalEq(expr);
            case "in":
                return evalIn(expr);
            case "and":
                return evalAnd(expr, alive);
            case "or":
                return evalOr(expr, alive);
            case "not":
                return evalNot(expr, alive);
            case "alive":
                return alive.copy();
            default:
                throw new IllegalArgumentException("不支持的表达式 op: " + op);
        }
    }

    private Bitmap evalEq(Map<String, Object> expr) {
        String col = requireString(expr, "col");
        Object value = expr.get("value");
        if (value == null) {
            throw new IllegalArgumentException("eq 表达式缺少 \"value\"");
        }
        return index.bitmapFor(col, normalizeValue(value));
    }

    private Bitmap evalIn(Map<String, Object> expr) {
        String col = requireString(expr, "col");
        Object valuesObj = expr.get("values");
        if (!(valuesObj instanceof List<?>)) {
            throw new IllegalArgumentException("in 表达式的 \"values\" 必须是数组");
        }
        Bitmap result = new Bitmap(index.rowCount());
        for (Object v : (List<?>) valuesObj) {
            if (v == null) {
                throw new IllegalArgumentException("in 的值不能为 null（缺失值请显式使用空字符串 \"\"）");
            }
            result = result.or(index.bitmapFor(col, normalizeValue(v)));
        }
        return result;
    }

    private Bitmap evalAnd(Map<String, Object> expr, Bitmap alive) {
        List<Object> args = requireArgs(expr);
        if (args.isEmpty()) {
            // AND 的单位元是全集；顶层查询会再与存活掩码求交
            return Bitmap.full(index.rowCount());
        }
        Bitmap result = null;
        for (Object a : args) {
            Bitmap b = eval(asExpr(a), alive);
            result = (result == null) ? b : result.and(b);
        }
        return result;
    }

    private Bitmap evalOr(Map<String, Object> expr, Bitmap alive) {
        List<Object> args = requireArgs(expr);
        Bitmap result = new Bitmap(index.rowCount());
        for (Object a : args) {
            result = result.or(eval(asExpr(a), alive));
        }
        return result;
    }

    private Bitmap evalNot(Map<String, Object> expr, Bitmap alive) {
        Object arg = expr.get("arg");
        if (arg == null) {
            throw new IllegalArgumentException("not 表达式缺少 \"arg\"");
        }
        Bitmap inner = eval(asExpr(arg), alive);
        // 关键语义：只在存活全集内取补，已删除行不会被取反出来
        return alive.andNot(inner);
    }

    /** 查询入口：求值并与存活掩码求交（双保险，确保任何写法都不会返回已删除行）。 */
    public Bitmap query(Map<String, Object> expr, Bitmap alive) {
        Bitmap result = eval(expr, alive);
        return result.and(alive);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asExpr(Object o) {
        if (!(o instanceof Map<?, ?>)) {
            throw new IllegalArgumentException("表达式节点必须是 JSON 对象: " + o);
        }
        return (Map<String, Object>) o;
    }

    private static List<Object> requireArgs(Map<String, Object> expr) {
        Object args = expr.get("args");
        if (args == null) {
            return List.of();
        }
        if (!(args instanceof List<?>)) {
            throw new IllegalArgumentException("表达式 \"" + expr.get("op") + "\" 的 \"args\" 必须是数组");
        }
        @SuppressWarnings("unchecked")
        List<Object> list = (List<Object>) args;
        return list;
    }

    private static String requireString(Map<String, Object> expr, String key) {
        Object v = expr.get(key);
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new IllegalArgumentException("表达式缺少非空字符串字段 \"" + key + "\"");
        }
        return (String) v;
    }

    /**
     * 把 JSON 标量值规范化为数据集中的字符串形式：
     * 布尔 -> "true"/"false"，整数 -> 十进制，浮点 -> Double.toString 规范形式。
     */
    static String normalizeValue(Object v) {
        if (v instanceof String s) {
            return s;
        }
        if (v instanceof Boolean b) {
            return Boolean.toString(b);
        }
        if (v instanceof Long l) {
            return Long.toString(l);
        }
        if (v instanceof Double d) {
            return Double.toString(d);
        }
        throw new IllegalArgumentException("不支持的取值类型: " + v.getClass().getSimpleName());
    }
}
