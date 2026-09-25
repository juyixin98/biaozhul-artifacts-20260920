package streamagg.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 类型工具：JSON 值在 Java 侧的映射为
 * {@code Map<String,Object>}（对象）、{@code List<Object>}（数组）、
 * {@code String}、{@code BigDecimal}（数字，精确）、{@code Boolean}、{@code null}。
 */
public final class Json {

    private Json() {
    }

    public static Map<String, Object> object() {
        return new LinkedHashMap<>();
    }

    public static String getString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof String s) {
            return s;
        }
        throw new JsonException("字段 " + key + " 应为字符串，实际为 " + v.getClass().getSimpleName());
    }

    public static String requireString(Map<String, Object> obj, String key) {
        String s = getString(obj, key);
        if (s == null || s.isBlank()) {
            throw new JsonException("缺少必填字符串字段: " + key);
        }
        return s;
    }

    public static Long getLong(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof java.math.BigDecimal bd) {
            long l = bd.longValueExact();
            return l;
        }
        throw new JsonException("字段 " + key + " 应为整数");
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object v) {
        if (v instanceof Map<?, ?> map) {
            return (Map<String, Object>) map;
        }
        throw new JsonException("应为 JSON 对象");
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object v) {
        if (v instanceof List<?> list) {
            return (List<Object>) list;
        }
        throw new JsonException("应为 JSON 数组");
    }
}
