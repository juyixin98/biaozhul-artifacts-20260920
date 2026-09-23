package orderedevents.json;

import java.util.List;
import java.util.Map;

/** Serializes the plain Java types produced by {@link Json} into JSON text. */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Number n) {
            writeNumber(sb, n);
        } else if (v instanceof Map<?, ?> map) {
            writeObject(sb, (Map<String, Object>) map);
        } else if (v instanceof List<?> list) {
            writeArray(sb, list);
        } else if (v instanceof Enum<?> e) {
            writeString(sb, e.name());
        } else {
            writeString(sb, v.toString());
        }
    }

    private static void writeObject(StringBuilder sb, Map<String, Object> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(sb, e.getKey());
            sb.append(':');
            writeValue(sb, e.getValue());
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<?> list) {
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeValue(sb, item);
        }
        sb.append(']');
    }

    private static void writeNumber(StringBuilder sb, Number n) {
        if (n instanceof Double d) {
            if (d.isNaN() || d.isInfinite()) {
                throw new IllegalArgumentException("non-finite double cannot be serialized");
            }
            long l = d.longValue();
            if (d == l) {
                sb.append(l);
            } else {
                sb.append(d);
            }
        } else if (n instanceof Float f) {
            if (f.isNaN() || f.isInfinite()) {
                throw new IllegalArgumentException("non-finite float cannot be serialized");
            }
            float fv = f;
            long l = (long) fv;
            if (fv == l) {
                sb.append(l);
            } else {
                sb.append(fv);
            }
        } else {
            sb.append(n);
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
}
