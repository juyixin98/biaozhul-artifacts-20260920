package sessions.json;

import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 生成器：把 Map/List/String/Number/Boolean/null 序列化为紧凑或缩进 JSON。
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
        append(sb, value, pretty, 0);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void append(StringBuilder sb, Object v, boolean pretty, int indent) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            string(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isFinite(d)) {
                sb.append(v);
            } else {
                throw new IllegalArgumentException("non-finite floating value cannot be JSON-encoded");
            }
        } else if (v instanceof Number) {
            sb.append(v);
        } else if (v instanceof Map<?, ?> map) {
            object(sb, (Map<String, Object>) map, pretty, indent);
        } else if (v instanceof List<?> list) {
            array(sb, list, pretty, indent);
        } else {
            throw new IllegalArgumentException(
                    "cannot encode type " + v.getClass().getName());
        }
    }

    private static void object(StringBuilder sb, Map<String, Object> map,
                               boolean pretty, int indent) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline(sb, pretty, indent + 1);
            string(sb, e.getKey());
            sb.append(pretty ? ": " : ":");
            append(sb, e.getValue(), pretty, indent + 1);
        }
        newline(sb, pretty, indent);
        sb.append('}');
    }

    private static void array(StringBuilder sb, List<?> list,
                              boolean pretty, int indent) {
        if (list.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline(sb, pretty, indent + 1);
            append(sb, item, pretty, indent + 1);
        }
        newline(sb, pretty, indent);
        sb.append(']');
    }

    private static void newline(StringBuilder sb, boolean pretty, int indent) {
        if (!pretty) {
            return;
        }
        sb.append('\n');
        sb.append("  ".repeat(indent));
    }

    private static void string(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
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
