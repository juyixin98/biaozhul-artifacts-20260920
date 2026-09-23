package tvl.core;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 不可变的带类型运行时值。
 *
 * 使用 Object 承载 long / Boolean / String，NULL 值统一为 Value.NULL。
 * 列中的值与表达式求值结果都使用该类型。
 */
public final class Value {
    public static final Value NULL = new Value(DataType.NULL, null);

    private final DataType type;
    private final Object data;

    private Value(DataType type, Object data) {
        this.type = type;
        this.data = data;
    }

    public static Value ofInteger(long v) {
        return new Value(DataType.INTEGER, v);
    }

    public static Value ofBoolean(boolean v) {
        return new Value(DataType.BOOLEAN, v);
    }

    public static Value ofString(String v) {
        return new Value(DataType.STRING, v);
    }

    public DataType type() {
        return type;
    }

    public boolean isNull() {
        return type == DataType.NULL;
    }

    public long integer() {
        return (Long) data;
    }

    public boolean bool() {
        return (Boolean) data;
    }

    public String string() {
        return (String) data;
    }

    /** 转换为 JSON 可序列化对象：long / boolean / string / null。 */
    public Object toJson() {
        return data;
    }

    public Map<String, Object> describe() {
        Map<String, Object> m = new LinkedHashMap<>();
        switch (type) {
            case NULL:
                m.put("type", "NULL");
                m.put("value", null);
                break;
            case INTEGER:
                m.put("type", "INTEGER");
                m.put("value", data);
                break;
            case BOOLEAN:
                m.put("type", "BOOLEAN");
                m.put("value", data);
                break;
            case STRING:
                m.put("type", "STRING");
                m.put("value", data);
                break;
        }
        return m;
    }

    @Override
    public String toString() {
        switch (type) {
            case NULL: return "NULL";
            case STRING: return "'" + ((String) data).replace("'", "''") + "'";
            default: return String.valueOf(data);
        }
    }
}
