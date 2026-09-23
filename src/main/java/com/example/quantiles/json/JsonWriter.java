package com.example.quantiles.json;

import com.example.quantiles.quantile.Fraction;

import java.util.List;
import java.util.Map;

/**
 * JSON 书写器。服务端用 Map/List/String/Number/Boolean/null 的 Java 表示构造输出，
 * 这里负责转义与缩进；{@link Fraction} 以其规范数字文本（整数或半整数）输出。
 */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Object value) {
        return write(value, false);
    }

    public static String writePretty(Object value) {
        return write(value, true);
    }

    public static String write(Object value, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value, pretty, 0);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v, boolean pretty, int depth) {
        if (v == null || v instanceof Json.JNull) {
            sb.append("null");
        } else if (v instanceof Json.JObject o) {
            writeObject(sb, o.members(), pretty, depth);
        } else if (v instanceof Map<?, ?> m) {
            writeObjectRaw(sb, m, pretty, depth);
        } else if (v instanceof Json.JArray a) {
            writeArray(sb, a.elements(), pretty, depth);
        } else if (v instanceof List<?> l) {
            writeArrayRaw(sb, l, pretty, depth);
        } else if (v instanceof Json.JString s) {
            writeString(sb, s.value());
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Json.JNumber n) {
            sb.append(n.raw());
        } else if (v instanceof Fraction f) {
            sb.append(f.toCanonicalString());
        } else if (v instanceof Integer || v instanceof Long || v instanceof Short
                || v instanceof Byte || v instanceof java.math.BigInteger) {
            sb.append(v);
        } else if (v instanceof Number n) {
            double d = n.doubleValue();
            if (!Double.isFinite(d)) {
                throw new JsonException("JSON 不支持非有限数字: " + d);
            }
            sb.append(n);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Json.JBool b) {
            sb.append(b.value());
        } else {
            throw new JsonException("不支持的输出类型: " + v.getClass().getName());
        }
    }

    private static void writeObject(StringBuilder sb, Map<String, Object> m,
                                    boolean pretty, int depth) {
        writeObjectRaw(sb, m, pretty, depth);
    }

    private static void writeObjectRaw(StringBuilder sb, Map<?, ?> m,
                                       boolean pretty, int depth) {
        if (m.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : m.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            if (pretty) {
                sb.append('\n');
                indent(sb, depth + 1);
            }
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(pretty ? ": " : ":");
            writeValue(sb, e.getValue(), pretty, depth + 1);
        }
        if (pretty) {
            sb.append('\n');
            indent(sb, depth);
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<Object> l,
                                   boolean pretty, int depth) {
        writeArrayRaw(sb, l, pretty, depth);
    }

    private static void writeArrayRaw(StringBuilder sb, List<?> l,
                                      boolean pretty, int depth) {
        if (l.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        boolean first = true;
        for (Object e : l) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            if (pretty) {
                sb.append('\n');
                indent(sb, depth + 1);
            }
            writeValue(sb, e, pretty, depth + 1);
        }
        if (pretty) {
            sb.append('\n');
            indent(sb, depth);
        }
        sb.append(']');
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

    private static void indent(StringBuilder sb, int depth) {
        sb.append("  ".repeat(depth));
    }
}
