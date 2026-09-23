package colscan.store;

/** 支持的列类型。当前仅实现数值类型（LONG / DOUBLE）。 */
public final class Types {

    public static final String LONG = "LONG";
    public static final String DOUBLE = "DOUBLE";

    private Types() {}

    public static boolean isValid(String type) {
        return LONG.equals(type) || DOUBLE.equals(type);
    }

    /** 比较两个同类型数值（null 不参与）。LONG 按精确长整型比较，DOUBLE 按浮点比较。 */
    @SuppressWarnings("unchecked")
    public static int compare(String type, Object a, Object b) {
        if (type.equals(LONG)) {
            return Long.compare(((Number) a).longValue(), ((Number) b).longValue());
        }
        return Double.compare(((Number) a).doubleValue(), ((Number) b).doubleValue());
    }
}
