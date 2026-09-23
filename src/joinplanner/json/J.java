package joinplanner.json;

import java.util.List;
import java.util.Map;

/**
 * Typed, null-safe accessors over parsed JSON (always LinkedHashMap/ArrayList).
 * Every failure is a {@link BadRequestException}-friendly IllegalArgumentException
 * carrying the full JSON path of the offending field.
 */
public final class J {

    private J() {
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object v, String path) {
        if (v instanceof Map<?, ?>) {
            return (Map<String, Object>) v;
        }
        throw new IllegalArgumentException(path + ": expected object");
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object v, String path) {
        if (v instanceof List<?>) {
            return (List<Object>) v;
        }
        throw new IllegalArgumentException(path + ": expected array");
    }

    public static Map<String, Object> fieldObj(Map<String, Object> m, String key, String path) {
        Object v = m.get(key);
        if (v == null) {
            throw new IllegalArgumentException(path + "." + key + ": required");
        }
        return obj(v, path + "." + key);
    }

    public static List<Object> fieldArr(Map<String, Object> m, String key, String path) {
        Object v = m.get(key);
        if (v == null) {
            throw new IllegalArgumentException(path + "." + key + ": required");
        }
        return arr(v, path + "." + key);
    }

    public static String str(Map<String, Object> m, String key, String path) {
        Object v = m.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new IllegalArgumentException(path + "." + key + ": expected non-empty string");
        }
        return s;
    }

    public static String optStr(Map<String, Object> m, String key, String dflt) {
        Object v = m.get(key);
        return v instanceof String s ? s : dflt;
    }

    public static double dbl(Object v, String path) {
        if (v instanceof Number n) {
            double d = n.doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException(path + ": must be finite");
            }
            return d;
        }
        throw new IllegalArgumentException(path + ": expected number");
    }

    public static double fieldDbl(Map<String, Object> m, String key, String path) {
        Object v = m.get(key);
        if (v == null) {
            throw new IllegalArgumentException(path + "." + key + ": required");
        }
        return dbl(v, path + "." + key);
    }

    public static Double optDbl(Map<String, Object> m, String key) {
        Object v = m.get(key);
        return v instanceof Number n ? n.doubleValue() : null;
    }

    public static long lng(Object v, String path) {
        if (v instanceof Number n) {
            double d = n.doubleValue();
            if (!Double.isFinite(d)) {
                throw new IllegalArgumentException(path + ": must be finite integer");
            }
            return n.longValue();
        }
        throw new IllegalArgumentException(path + ": expected integer");
    }

    public static Long optLng(Map<String, Object> m, String key) {
        Object v = m.get(key);
        return v instanceof Number n ? n.longValue() : null;
    }

    public static boolean fieldBool(Map<String, Object> m, String key, boolean dflt) {
        Object v = m.get(key);
        return v instanceof Boolean b ? b : dflt;
    }
}
