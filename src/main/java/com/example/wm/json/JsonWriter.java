package com.example.wm.json;

import java.util.Collection;
import java.util.Map;

/** 极简 JSON 序列化（仅依赖 JDK）。输入为 Map / Collection / 标量。 */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Number || v instanceof Character) {
            sb.append(v);
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) m).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Collection<?> c) {
            sb.append('[');
            boolean first = true;
            for (Object o : c) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeTo(sb, o);
            }
            sb.append(']');
        } else if (v.getClass().isArray()) {
            writeTo(sb, java.util.Arrays.asList((Object[]) v));
        } else {
            throw new JsonException("cannot serialize type " + v.getClass().getName());
        }
    }

    @SuppressWarnings("unchecked")
    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) m).entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writeString(sb, e.getKey());
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (v instanceof Collection<?> c) {
            if (c.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object o : c) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writePretty(sb, o, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, v);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
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
}
