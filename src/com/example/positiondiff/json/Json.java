package com.example.positiondiff.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Serializes Java values ({@link Map}, {@link List}, String, Number, Boolean,
 * null) to JSON, and provides small typed accessors for reading parsed input.
 */
public final class Json {

    private Json() {}

    public static Map<String, Object> obj() {
        return new LinkedHashMap<>();
    }

    public static List<Object> arr() {
        return new ArrayList<>();
    }

    /** Pretty-printed JSON (2-space indentation). */
    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object value, int indent) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            writeNumber(sb, n);
        } else if (value instanceof String str) {
            writeString(sb, str);
        } else if (value instanceof Map<?, ?> map) {
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                write(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (value instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object item : list) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                write(sb, item, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot serialize " + value.getClass());
        }
    }

    private static void writeNumber(StringBuilder sb, Number n) {
        double d = n.doubleValue();
        if (n instanceof Double || n instanceof Float) {
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("non-finite number in JSON");
            }
            if (d == Math.rint(d) && !Double.isInfinite(d)
                    && d >= -9.007199254740992E15 && d <= 9.007199254740992E15) {
                sb.append(Long.toString((long) d));
            } else {
                sb.append(Double.toString(d));
            }
        } else {
            sb.append(n.toString());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
    }

    // ---- typed accessors over parsed values ----

    public static String getString(Map<String, Object> map, String key) {
        Object v = map.get(key);
        if (v == null) return null;
        if (!(v instanceof String)) {
            throw new IllegalArgumentException("field '" + key + "' must be a string");
        }
        return (String) v;
    }

    public static String requireString(Map<String, Object> map, String key) {
        String v = getString(map, key);
        if (v == null) {
            throw new IllegalArgumentException("missing required field '" + key + "'");
        }
        return v;
    }

    public static long getLong(Map<String, Object> map, String key, long dflt) {
        Object v = map.get(key);
        if (v == null) return dflt;
        if (v instanceof Number n) return n.longValue();
        throw new IllegalArgumentException("field '" + key + "' must be a number");
    }

    public static boolean getBool(Map<String, Object> map, String key, boolean dflt) {
        Object v = map.get(key);
        if (v == null) return dflt;
        if (v instanceof Boolean b) return b;
        throw new IllegalArgumentException("field '" + key + "' must be a boolean");
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> getObject(Map<String, Object> map, String key) {
        Object v = map.get(key);
        if (v == null) return null;
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("field '" + key + "' must be an object");
        }
        return (Map<String, Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> getArray(Map<String, Object> map, String key) {
        Object v = map.get(key);
        if (v == null) return null;
        if (!(v instanceof List)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array");
        }
        return (List<Object>) v;
    }
}
