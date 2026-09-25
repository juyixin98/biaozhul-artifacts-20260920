package com.example.ac.server;

import java.util.Collection;
import java.util.Map;

/** 极简 JSON 序列化器（null、Boolean、Number、String、Map、Collection、数组、对象 record）。 */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, v);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean || v instanceof Number) {
            sb.append(v);
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Collection<?> col) {
            writeArray(sb, col.toArray());
        } else if (v.getClass().isArray()) {
            writeReflectArray(sb, v);
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void writeArray(StringBuilder sb, Object[] items) {
        sb.append('[');
        for (int i = 0; i < items.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            writeValue(sb, items[i]);
        }
        sb.append(']');
    }

    private static void writeReflectArray(StringBuilder sb, Object array) {
        int n = java.lang.reflect.Array.getLength(array);
        sb.append('[');
        for (int i = 0; i < n; i++) {
            if (i > 0) {
                sb.append(',');
            }
            writeValue(sb, java.lang.reflect.Array.get(array, i));
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
}
