package windowengine;

import java.util.Objects;

/**
 * 单元格值。引擎只支持三种值：64 位有符号整数（LONG）、字符串（STRING）、NULL。
 * 不支持浮点、布尔等类型；JSON 请求里出现小数会被拒绝（见 Json / RequestParser）。
 */
public final class Value {

    public enum Type { LONG, STRING, NULL }

    public static final Value NULL = new Value(Type.NULL, null);

    private final Type type;
    private final Object data;

    private Value(Type type, Object data) {
        this.type = type;
        this.data = data;
    }

    public static Value ofLong(long v) {
        return new Value(Type.LONG, v);
    }

    public static Value ofString(String v) {
        if (v == null) {
            return NULL;
        }
        return new Value(Type.STRING, v);
    }

    public Type type() {
        return type;
    }

    public boolean isNull() {
        return type == Type.NULL;
    }

    public long asLong() {
        if (type != Type.LONG) {
            throw new EngineException(ErrorCode.TYPE_MISMATCH,
                    "期望 LONG，实际为 " + type);
        }
        return (Long) data;
    }

    public String asString() {
        if (type != Type.STRING) {
            throw new EngineException(ErrorCode.TYPE_MISMATCH,
                    "期望 STRING，实际为 " + type);
        }
        return (String) data;
    }

    /**
     * 相等判定（用于分区键分组与并列键判断）：
     * NULL 与 NULL 相等；LONG 与 STRING 互不相等。
     */
    public boolean valueEquals(Value other) {
        if (this.type == Type.NULL || other.type == Type.NULL) {
            return this.type == other.type;
        }
        if (this.type != other.type) {
            return false;
        }
        return Objects.equals(this.data, other.data);
    }

    /**
     * 比较两个非 NULL 值。仅在同类型、可比较时返回结果；
     * 不同类型（LONG 与 STRING）抛出 TYPE_MISMATCH。
     */
    @SuppressWarnings("unchecked")
    public int compareNonNull(Value other) {
        if (this.type != other.type) {
            throw new EngineException(ErrorCode.TYPE_MISMATCH,
                    "无法比较 " + this.type + " 与 " + other.type);
        }
        if (this.type == Type.LONG) {
            return Long.compare((Long) data, (Long) other.data);
        }
        return ((Comparable<String>) data).compareTo((String) other.data);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Value other)) {
            return false;
        }
        return valueEquals(other);
    }

    @Override
    public int hashCode() {
        return type == Type.NULL ? 0 : Objects.hash(type, data);
    }

    @Override
    public String toString() {
        return switch (type) {
            case NULL -> "NULL";
            case LONG -> data.toString();
            case STRING -> "'" + data + "'";
        };
    }
}
