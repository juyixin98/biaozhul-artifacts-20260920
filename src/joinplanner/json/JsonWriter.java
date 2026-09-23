package joinplanner.json;

import java.util.Collection;
import java.util.Map;

/**
 * Small JSON serializer for the Java types produced by {@link JsonParser}.
 * Supports pretty-printing and NaN/Infinity (emitted as strings, which JSON
 * parsers keep readable even though strict JSON has no such numbers).
 */
public final class JsonWriter {

    private final StringBuilder sb = new StringBuilder();
    private final String indent;
    private int level;

    private JsonWriter(String indent) {
        this.indent = indent;
    }

    public static String write(Object value) {
        return new JsonWriter(null).value(value).toString();
    }

    public static String writePretty(Object value) {
        return new JsonWriter("  ").value(value).toString();
    }

    @Override
    public String toString() {
        return sb.toString();
    }

    private JsonWriter value(Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Number n) {
            number(n);
        } else if (v instanceof String str) {
            string(str);
        } else if (v instanceof Map<?, ?> map) {
            object(map);
        } else if (v instanceof Collection<?> col) {
            array(col);
        } else if (v instanceof Object[] arr) {
            array(java.util.Arrays.asList(arr));
        } else {
            throw new IllegalArgumentException("Cannot serialize " + v.getClass());
        }
        return this;
    }

    private void number(Number n) {
        double d = n.doubleValue();
        if (Double.isNaN(d) || Double.isInfinite(d)) {
            // Strict JSON cannot express these; quote them so output stays valid-ish and readable.
            string(Double.toString(d));
            return;
        }
        if (n instanceof Double || n instanceof Float) {
            if (d == Math.rint(d) && !Double.isInfinite(d) && Math.abs(d) < 1e16) {
                // keep integer-valued doubles tidy
                sb.append((long) d);
            } else {
                sb.append(d);
            }
        } else {
            sb.append(n);
        }
    }

    private void string(String s) {
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

    private void object(Map<?, ?> map) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        level++;
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline();
            string(String.valueOf(e.getKey()));
            sb.append(indent == null ? ":" : ": ");
            value(e.getValue());
        }
        level--;
        newline();
        sb.append('}');
    }

    private void array(Collection<?> col) {
        if (col.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        level++;
        boolean first = true;
        for (Object item : col) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline();
            value(item);
        }
        level--;
        newline();
        sb.append(']');
    }

    private void newline() {
        if (indent == null) {
            return;
        }
        sb.append('\n');
        sb.append(indent.repeat(level));
    }
}
