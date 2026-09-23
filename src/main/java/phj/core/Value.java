package phj.core;

import java.util.Objects;

/**
 * 标量值。支持五种类型：NULL / LONG / DOUBLE / STRING / BOOLEAN。
 *
 * 相等语义（用于连接判定）：
 *  - NULL 与任何值（含 NULL）都不相等 —— SQL 三值逻辑；
 *  - LONG 与 DOUBLE 按数值比较（1 与 1.0 相等）；
 *  - STRING 大小写敏感；BOOLEAN 只与 BOOLEAN 比较；
 *  - 跨族（数值 vs 字符串 vs 布尔）不相等。
 *
 * 注：LONG/DOUBLE 跨型比较走 double，53 位尾数以远的极端 long 精度不保证，
 * 对连接键场景与 SQL 引擎（double 键）行为一致。
 */
public final class Value {

    public enum Type { NULL, LONG, DOUBLE, STRING, BOOLEAN }

    public static final Value NULL = new Value(Type.NULL, null);

    public final Type type;
    private final Object data;

    private Value(Type type, Object data) {
        this.type = type;
        this.data = data;
    }

    public static Value ofLong(long v) { return new Value(Type.LONG, v); }
    public static Value ofDouble(double v) { return new Value(Type.DOUBLE, v); }
    public static Value ofString(String v) { return new Value(Type.STRING, Objects.requireNonNull(v)); }
    public static Value ofBool(boolean v) { return new Value(Type.BOOLEAN, v); }

    public boolean isNull() { return type == Type.NULL; }
    public long asLong() { return (Long) data; }
    public double asDouble() { return (Double) data; }
    public String asString() { return (String) data; }
    public boolean asBool() { return (Boolean) data; }

    /** 从自写 JSON 解析出的 Java 对象构造 Value。 */
    public static Value fromJson(Object o) {
        if (o == null) return NULL;
        if (o instanceof Long l) return ofLong(l);
        if (o instanceof Integer i) return ofLong(i.longValue());
        if (o instanceof Double d) return ofDouble(d);
        if (o instanceof String s) return ofString(s);
        if (o instanceof Boolean b) return ofBool(b);
        throw new IllegalArgumentException("不支持的 JSON 标量类型：" + o.getClass().getSimpleName());
    }

    public Object toJson() {
        return switch (type) {
            case NULL -> null;
            case LONG -> data;
            case DOUBLE -> data;
            case STRING -> data;
            case BOOLEAN -> data;
        };
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Value v)) return false;
        // 注意：不能因为 this==o 就提前返回 true —— Value.NULL 是共享单例，
        // SQL 语义下 NULL 与 NULL 也不相等（这也保证含 NULL 的 Row 多重集哈希正确）。
        if (type == Type.NULL || v.type == Type.NULL) return false;
        if (type == Type.LONG && v.type == Type.LONG) return asLong() == v.asLong();
        if (type == Type.DOUBLE && v.type == Type.DOUBLE) {
            // 归一化 ±0.0（与跨型分支保持一致），NaN 经 == 自然不等。
            double a = asDouble(), b = v.asDouble();
            return (a == 0.0 ? 0.0 : a) == (b == 0.0 ? 0.0 : b);
        }
        if ((type == Type.LONG || type == Type.DOUBLE)
                && (v.type == Type.LONG || v.type == Type.DOUBLE)) {
            // 数值跨型比较：double 为范围内精确整数时按整数比，否则按 double 比。
            double da = numericDouble();
            double db = v.numericDouble();
            if (da == Math.rint(da) && db == Math.rint(db)
                    && da >= Long.MIN_VALUE && da <= Long.MAX_VALUE
                    && db >= Long.MIN_VALUE && db <= Long.MAX_VALUE) {
                return (long) da == (long) db;
            }
            return Double.compare(da, db) == 0;
        }
        if (type != v.type) return false;
        return data.equals(v.data);
    }

    @Override
    public int hashCode() {
        return switch (type) {
            case NULL -> 0;
            case BOOLEAN -> ((Boolean) data) ? 1231 : 1237;
            case STRING -> data.hashCode();
            // 数值族统一按 double 位模式哈希：LONG 与 DOUBLE 跨型 equals 时哈希必然一致
            // （equals 成立可推出两者归一化后的 double 位相同，NaN 亦自洽；
            // +0.0/-0.0 在 equals 中相等，这里一并归一化）。
            case LONG -> Double.hashCode(normalizeZero((double) asLong()));
            case DOUBLE -> Double.hashCode(normalizeZero(asDouble()));
        };
    }

    private static double normalizeZero(double d) { return d == 0.0 ? 0.0 : d; }

    private double numericDouble() {
        return type == Type.LONG ? (double) asLong() : asDouble();
    }

    /**
     * 数据语义哈希：与 Row.equals 的“NULL=NULL、LONG/DOUBLE 按数值相等”配套。
     * 不用于连接哈希表（那里走 hashCode + Key 的 SQL 语义）。
     */
    public int dataHashCode() {
        return switch (type) {
            case NULL -> 0;
            case BOOLEAN -> ((Boolean) data) ? 1231 : 1237;
            case STRING -> data.hashCode();
            case LONG, DOUBLE -> Double.hashCode(normalizeZero(numericDouble()));
        };
    }

    @Override
    public String toString() {
        return switch (type) {
            case NULL -> "NULL";
            case DOUBLE -> String.valueOf(asDouble());
            default -> data.toString();
        };
    }
}
