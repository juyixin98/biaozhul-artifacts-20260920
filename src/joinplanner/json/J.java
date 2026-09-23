package joinplanner.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Typed accessors over the Object graph produced by {@link JsonParser}.
 * Every accessor throws {@link BadInputException} (mapped to HTTP 400) when the
 * actual node type differs from the expected type, so request validation is
 * enforced at the point of field access.
 */
public final class J {

    private J() {}

    public static Map<String, Object> obj(Object node, String where) {
        if (node instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) node;
            return m;
        }
        throw new BadInputException(where + " must be a JSON object");
    }

    public static List<Object> arr(Object node, String where) {
        if (node instanceof List) {
            @SuppressWarnings("unchecked")
            List<Object> a = (List<Object>) node;
            return a;
        }
        throw new BadInputException(where + " must be a JSON array");
    }

    public static String str(Object node, String where) {
        if (node instanceof String) {
            return (String) node;
        }
        throw new BadInputException(where + " must be a string");
    }

    public static boolean bool(Object node, String where) {
        if (node instanceof Boolean) {
            return (Boolean) node;
        }
        throw new BadInputException(where + " must be a boolean");
    }

    public static double num(Object node, String where) {
        if (node instanceof Number) {
            double d = ((Number) node).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new BadInputException(where + " must be a finite number");
            }
            return d;
        }
        throw new BadInputException(where + " must be a number");
    }

    /** Integer in [Integer.MIN_VALUE, Integer.MAX_VALUE]. */
    public static int integer(Object node, String where) {
        double d = num(node, where);
        if (!Double.isFinite(d) || d != Math.rint(d) || d < Integer.MIN_VALUE || d > Integer.MAX_VALUE) {
            throw new BadInputException(where + " must be an integer");
        }
        return (int) d;
    }

    /** Non-negative integer; max enforced by caller. */
    public static int nonNegInt(Object node, String where) {
        int v = integer(node, where);
        if (v < 0) {
            throw new BadInputException(where + " must be >= 0");
        }
        return v;
    }

    public static double nonNegNum(Object node, String where) {
        double d = num(node, where);
        if (d < 0) {
            throw new BadInputException(where + " must be >= 0");
        }
        return d;
    }

    public static Object get(Map<String, Object> m, String key) {
        return m.get(key);
    }

    public static Map<String, Object> getObj(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return obj(v, "'" + key + "'");
    }

    public static List<Object> getArr(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return arr(v, "'" + key + "'");
    }

    public static String getStr(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return str(v, "'" + key + "'");
    }

    public static Integer getInt(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return integer(v, "'" + key + "'");
    }

    public static Boolean getBool(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return bool(v, "'" + key + "'");
    }

    public static Double getNum(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        return num(v, "'" + key + "'");
    }

    public static Map<String, Object> newObj() {
        return new LinkedHashMap<>();
    }
}
