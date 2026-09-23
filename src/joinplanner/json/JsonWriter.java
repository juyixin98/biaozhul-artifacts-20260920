package joinplanner.json;

import java.util.List;
import java.util.Map;

/** Serializes the Object graph (Map/List/String/Double/Boolean/null) back to JSON. */
public final class JsonWriter {

    private JsonWriter() {}

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Double) {
            sb.append(formatNumber((Double) v));
        } else if (v instanceof Integer) {
            sb.append(v);
        } else if (v instanceof Long) {
            sb.append(v);
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) v) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void writeIndented(StringBuilder sb, Object v, int depth) {
        if (v instanceof Map) {
            Map<?, ?> map = (Map<?, ?>) v;
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, depth + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writeIndented(sb, e.getValue(), depth + 1);
            }
            sb.append('\n');
            indent(sb, depth);
            sb.append('}');
        } else if (v instanceof List) {
            List<?> list = (List<?>) v;
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, depth + 1);
                writeIndented(sb, item, depth + 1);
            }
            sb.append('\n');
            indent(sb, depth);
            sb.append(']');
        } else {
            writeValue(sb, v);
        }
    }

    private static void indent(StringBuilder sb, int depth) {
        for (int i = 0; i < depth; i++) {
            sb.append("  ");
        }
    }

    /** Integers render without a trailing ".0"; very large/small values use exponent form. */
    public static String formatNumber(double d) {
        if (d == Math.rint(d) && !Double.isInfinite(d) && Math.abs(d) < 1e15) {
            return Long.toString((long) d);
        }
        return Double.toString(d);
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }
}
