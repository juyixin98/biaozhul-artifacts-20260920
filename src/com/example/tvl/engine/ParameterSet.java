package com.example.tvl.engine;

import com.example.tvl.sql.DataType;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 参数绑定集合。绑定值在请求中以 {"type": "...", "value": ...} 给出。
 *
 * 严格类型：JSON 字符串不会被转成数字，带小数的数字不能绑定给 INTEGER。
 */
public final class ParameterSet {

    private final List<Value> values;

    private ParameterSet(List<Value> values) {
        this.values = List.copyOf(values);
    }

    public int size() {
        return values.size();
    }

    public Value get(int index) {
        return values.get(index);
    }

    /**
     * 按占位符出现顺序绑定。
     *
     * @param expectedCount SQL 中 '?' 的数量
     * @param raw           请求中的 params 数组（可为 null）
     */
    public static ParameterSet bind(int expectedCount, List<?> raw) {
        int provided = raw == null ? 0 : raw.size();
        if (provided != expectedCount) {
            throw new SemanticException(
                    "参数数量不匹配：SQL 需要 " + expectedCount + " 个，实际提供 " + provided + " 个");
        }
        List<Value> values = new ArrayList<>(expectedCount);
        for (int i = 0; i < expectedCount; i++) {
            values.add(coerce(raw.get(i), i));
        }
        return new ParameterSet(values);
    }

    private static Value coerce(Object binding, int index) {
        if (!(binding instanceof Map<?, ?> m)) {
            throw new SemanticException("params[" + index + "] 必须是 {type, value} 对象");
        }
        Object typeRaw = m.get("type");
        if (!(typeRaw instanceof String typeStr)) {
            throw new SemanticException("params[" + index + "] 缺少字符串类型的 type");
        }
        DataType type;
        try {
            type = DataType.parse(typeStr);
        } catch (IllegalArgumentException e) {
            throw new SemanticException("params[" + index + "] 类型非法: " + typeRaw);
        }
        Object v = m.get("value");
        if (v == null) {
            return new Value(type, null);
        }
        Object converted = switch (type) {
            case INTEGER -> toLong(v, index);
            case FLOAT -> toDouble(v, index);
            case TEXT -> {
                if (!(v instanceof String s)) {
                    throw new SemanticException(
                            "params[" + index + "] 声明为 TEXT，但值不是 JSON 字符串（拒绝隐式数值转字符串）: "
                                    + describe(v));
                }
                yield s;
            }
        };
        return new Value(type, converted);
    }

    private static Long toLong(Object v, int index) {
        if (v instanceof Long l) {
            return l;
        }
        if (v instanceof Integer i) {
            return i.longValue();
        }
        if (v instanceof Double d) {
            if (d.isNaN() || d.isInfinite() || d != Math.rint(d) || d < Long.MIN_VALUE || d > Long.MAX_VALUE) {
                throw new SemanticException(
                        "params[" + index + "] 声明为 INTEGER，但值不是整数: " + d);
            }
            return d.longValue();
        }
        if (v instanceof String) {
            throw new SemanticException(
                    "params[" + index + "] 声明为 INTEGER，但收到字符串（拒绝隐式字符串转数值）");
        }
        throw new SemanticException("params[" + index + "] 无法作为 INTEGER: " + describe(v));
    }

    private static Double toDouble(Object v, int index) {
        if (v instanceof Number n) {
            double d = n.doubleValue();
            if (d == Double.POSITIVE_INFINITY || d == Double.NEGATIVE_INFINITY) {
                throw new SemanticException("params[" + index + "] FLOAT 值超出有限范围");
            }
            return d;
        }
        if (v instanceof String) {
            throw new SemanticException(
                    "params[" + index + "] 声明为 FLOAT，但收到字符串（拒绝隐式字符串转数值）");
        }
        throw new SemanticException("params[" + index + "] 无法作为 FLOAT: " + describe(v));
    }

    private static String describe(Object v) {
        return v == null ? "null" : v.getClass().getSimpleName() + "(" + v + ")";
    }
}
