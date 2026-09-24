package com.tvl.types;

/**
 * 强类型标量值：包装 Long / Double / String / Boolean 中的一种，value 可为 null（SQL NULL）。
 * 不同类型之间的比较规则集中在这里，保证逐行解释器与向量执行器走完全一致的语义。
 */
public final class Values {

    private Values() {
    }

    public static Object nullDefault(DataType type) {
        switch (type) {
            case INTEGER:
                return 0L;
            case DOUBLE:
                return 0.0d;
            case STRING:
                return "";
            case BOOLEAN:
                return Boolean.FALSE;
            default:
                throw new IllegalStateException("未覆盖类型: " + type);
        }
    }

    public static long getLong(Object value) {
        return ((Number) value).longValue();
    }

    public static double getDouble(Object value) {
        return ((Number) value).doubleValue();
    }

    /**
     * 把外部（JSON）传入的标量按声明类型归一化。
     * 任何不匹配都拒绝 —— 特别地，字符串 "1" 不会被当成数值 1。
     *
     * @param declared 期望类型
     * @param raw      原始 Java 对象（Long/Double/String/Boolean 或 null）
     * @param what     出错时用于定位（如 "参数#1"、"列 x 第 3 行"）
     */
    public static Object coerce(DataType declared, Object raw, String what) {
        if (raw == null) {
            return null;
        }
        switch (declared) {
            case INTEGER:
                if (raw instanceof Long) {
                    return raw;
                }
                if (raw instanceof Integer || raw instanceof Short || raw instanceof Byte) {
                    return ((Number) raw).longValue();
                }
                if (raw instanceof Double) {
                    double d = (Double) raw;
                    if (!Double.isFinite(d) || d != Math.rint(d)) {
                        throw new TypeCheckException(
                                what + " 声明为 INTEGER，但收到带小数/非有限的数值 " + raw);
                    }
                    return (long) d;
                }
                break;
            case DOUBLE:
                if (raw instanceof Number) {
                    return ((Number) raw).doubleValue();
                }
                break;
            case STRING:
                if (raw instanceof String) {
                    return raw;
                }
                break;
            case BOOLEAN:
                if (raw instanceof Boolean) {
                    return raw;
                }
                break;
            default:
                break;
        }
        throw new TypeCheckException(what + " 期望类型 " + declared.sqlName()
                + "，但收到 " + raw.getClass().getSimpleName() + "(" + raw + ")；"
                + "本系统不做字符串/数值/布尔之间的隐式转换");
    }
}
