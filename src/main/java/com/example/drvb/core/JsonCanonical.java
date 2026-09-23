package com.example.drvb.core;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Minimal deterministic JSON serializer for content checksums.
 *
 * <p>The core package deliberately has no Jackson dependency, so checksum
 * canonicalization is implemented here with strict rules:
 * <ul>
 *   <li>map keys are sorted lexicographically (UTF-16 code-unit order, which
 *       for ASCII rule definitions matches ASCII order);</li>
 *   <li>list order is preserved (rule order is semantically meaningful);</li>
 *   <li>strings use standard JSON escaping;</li>
 *   <li>numbers are rendered via {@code toString()} of the JDK number — rule
 *       values come from JSON parsing, so integral types stay integral.</li>
 * </ul>
 */
final class JsonCanonical {

    private JsonCanonical() {
    }

    static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(value, sb);
        return sb.toString();
    }

    private static void writeValue(Object value, StringBuilder sb) {
        switch (value) {
            case null -> sb.append("null");
            case Boolean b -> sb.append(b.booleanValue());
            case Map<?, ?> map -> writeMap(map, sb);
            case List<?> list -> writeList(list, sb);
            case Number n -> sb.append(n);
            default -> writeString(value.toString(), sb);
        }
    }

    private static void writeMap(Map<?, ?> map, StringBuilder sb) {
        TreeMap<String, Object> sorted = new TreeMap<>();
        for (Map.Entry<?, ?> e : map.entrySet()) {
            sorted.put(e.getKey().toString(), e.getValue());
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> e : sorted.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(e.getKey(), sb);
            sb.append(':');
            writeValue(e.getValue(), sb);
        }
        sb.append('}');
    }

    private static void writeList(List<?> list, StringBuilder sb) {
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeValue(item, sb);
        }
        sb.append(']');
    }

    private static void writeString(String s, StringBuilder sb) {
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
