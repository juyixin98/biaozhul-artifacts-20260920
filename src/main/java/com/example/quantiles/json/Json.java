package com.example.quantiles.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小 JSON 值模型（仅用于本服务的输入输出，零外部依赖）。
 * 数字统一以 {@link Double} 承载，读取整数时由 {@link Json#asLong(Object)} 精确转换。
 */
public sealed interface Json permits Json.JObject, Json.JArray, Json.JString, Json.JNumber, Json.JBool, Json.JNull {

    record JObject(Map<String, Object> members) implements Json {
        public JObject {
            members = new LinkedHashMap<>(members);
        }
    }

    record JArray(List<Object> elements) implements Json {
        public JArray {
            elements = List.copyOf(elements);
        }
    }

    record JString(String value) implements Json {
    }

    /** 以词法文本保留数字，避免 1234567890123456 这类长整数在 double 上丢精度。 */
    record JNumber(String raw) implements Json {
    }

    record JBool(boolean value) implements Json {
    }

    record JNull() implements Json {
    }

    /* ---------- 类型安全的读取辅助（同时接受解析模型与原始 Java 类型） ---------- */

    @SuppressWarnings("unchecked")
    static Map<String, Object> asObject(Object v) {
        if (v instanceof JObject o) {
            return o.members();
        }
        if (v instanceof Map<?, ?> m) {
            return (Map<String, Object>) m;
        }
        throw new JsonException("期望 object，实际为 " + typeName(v));
    }

    @SuppressWarnings("unchecked")
    static List<Object> asArray(Object v) {
        if (v instanceof JArray a) {
            return a.elements();
        }
        if (v instanceof List<?> l) {
            return (List<Object>) l;
        }
        throw new JsonException("期望 array，实际为 " + typeName(v));
    }

    static String asString(Object v) {
        if (v instanceof JString s) {
            return s.value();
        }
        if (v instanceof String s) {
            return s;
        }
        throw new JsonException("期望 string，实际为 " + typeName(v));
    }

    static double asDouble(Object v) {
        if (v instanceof JNumber n) {
            return Double.parseDouble(n.raw());
        }
        if (v instanceof Number n) {
            return n.doubleValue();
        }
        throw new JsonException("期望 number，实际为 " + typeName(v));
    }

    /** 数字转 long；带非零小数部分将报错。 */
    static long asLong(Object v) {
        double d;
        if (v instanceof JNumber n) {
            d = Double.parseDouble(n.raw());
        } else if (v instanceof Number n) {
            d = n.doubleValue();
        } else {
            throw new JsonException("期望 number，实际为 " + typeName(v));
        }
        if (!Double.isFinite(d) || d != Math.rint(d)) {
            throw new JsonException("期望整数值，实际为 " + v);
        }
        return (long) d;
    }

    static boolean asBool(Object v) {
        if (v instanceof JBool b) {
            return b.value();
        }
        if (v instanceof Boolean b) {
            return b;
        }
        throw new JsonException("期望 boolean，实际为 " + typeName(v));
    }

    static boolean isNull(Object v) {
        return v instanceof JNull;
    }

    static String typeName(Object v) {
        if (v == null) {
            return "null";
        }
        if (v instanceof JObject) {
            return "object";
        }
        if (v instanceof JArray) {
            return "array";
        }
        if (v instanceof JString) {
            return "string";
        }
        if (v instanceof JNumber) {
            return "number";
        }
        if (v instanceof JBool) {
            return "boolean";
        }
        if (v instanceof JNull) {
            return "null";
        }
        return v.getClass().getSimpleName();
    }
}
