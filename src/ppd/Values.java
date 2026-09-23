package ppd;

/** 值的三值逻辑与比较运算工具（SQL 语义：UNKNOWN 用 null 表示）。 */
public final class Values {

    private Values() {}

    public static boolean isNull(Object v) { return v == null; }

    public static boolean isNumber(Object v) {
        return v instanceof Long || v instanceof Integer || v instanceof Double;
    }

    public static double asDouble(Object v) {
        return ((Number) v).doubleValue();
    }

    public static String typeName(Object v) {
        if (v == null) return "NULL";
        if (v instanceof Long || v instanceof Integer) return "INTEGER";
        if (v instanceof Double) return "DOUBLE";
        if (v instanceof String) return "STRING";
        if (v instanceof Boolean) return "BOOLEAN";
        return v.getClass().getSimpleName();
    }

    /** SQL = ；null 参与 => null。数值按数值比较，其余按 equals；跨类型 => false。 */
    public static Boolean eq(Object a, Object b) {
        if (a == null || b == null) return null;
        if (isNumber(a) && isNumber(b)) return numericEqual(a, b);
        if (!a.getClass().equals(b.getClass())) return false;
        return a.equals(b);
    }

    public static boolean numericEqual(Object a, Object b) {
        if (a instanceof Long && b instanceof Long) return a.equals(b);
        if (integral(a) && integral(b)) {
            return ((Number) a).longValue() == ((Number) b).longValue();
        }
        return ((Number) a).doubleValue() == ((Number) b).doubleValue();
    }

    private static boolean integral(Object v) {
        return v instanceof Long || v instanceof Integer;
    }

    /** SQL 排序比较：返回 -1/0/1；null 输入返回 null；不可比较类型抛错。 */
    public static Integer compare(Object a, Object b) {
        if (a == null || b == null) return null;
        if (isNumber(a) && isNumber(b)) {
            if (integral(a) && integral(b)) {
                return Long.compare(((Number) a).longValue(), ((Number) b).longValue());
            }
            return Double.compare(((Number) a).doubleValue(), ((Number) b).doubleValue());
        }
        if (a instanceof String && b instanceof String) {
            return ((String) a).compareTo((String) b);
        }
        throw new EngineException("无法比较 " + typeName(a) + " 与 " + typeName(b));
    }

    public static Boolean and(Boolean a, Boolean b) {
        if (Boolean.FALSE.equals(a) || Boolean.FALSE.equals(b)) return false;
        if (a == null || b == null) return null;
        return true;
    }

    public static Boolean or(Boolean a, Boolean b) {
        if (Boolean.TRUE.equals(a) || Boolean.TRUE.equals(b)) return true;
        if (a == null || b == null) return null;
        return false;
    }

    public static Boolean not(Boolean a) { return a == null ? null : !a; }

    public static Object add(Object a, Object b) {
        if (a == null || b == null) return null;
        if (a instanceof String || b instanceof String) {
            return String.valueOf(a) + String.valueOf(b);
        }
        return numericOp(a, b, (x, y) -> x + y);
    }

    public static Object subtract(Object a, Object b) {
        if (a == null || b == null) return null;
        return numericOp(a, b, (x, y) -> x - y);
    }

    public static Object multiply(Object a, Object b) {
        if (a == null || b == null) return null;
        return numericOp(a, b, (x, y) -> x * y);
    }

    private interface NumOp { double apply(double x, double y); }

    private static Object numericOp(Object a, Object b, NumOp op) {
        if (!isNumber(a) || !isNumber(b)) {
            throw new EngineException("算术运算要求数值，得到 " + typeName(a) + ", " + typeName(b));
        }
        double r = op.apply(asDouble(a), asDouble(b));
        if (integral(a) && integral(b) && r == Math.rint(r)
                && r >= Long.MIN_VALUE && r <= Long.MAX_VALUE) {
            return (long) r;
        }
        return r;
    }
}
