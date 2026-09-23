package colscan;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser and serializer using only the JDK.
 * Parsed types: Map (LinkedHashMap, preserves order), List, String,
 * Long, Double, Boolean, null.
 */
public final class Json {

    private final String s;
    private int p;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        Json j = new Json(text);
        j.ws();
        Object v = j.value();
        j.ws();
        if (j.p != j.s.length()) {
            throw new IllegalArgumentException("trailing characters at " + j.p);
        }
        return v;
    }

    private Object value() {
        ws();
        if (p >= s.length()) {
            throw new IllegalArgumentException("unexpected end of input");
        }
        char c = s.charAt(p);
        switch (c) {
            case '{': return object();
            case '[': return array();
            case '"': return string();
            case 't': expect("true"); return Boolean.TRUE;
            case 'f': expect("false"); return Boolean.FALSE;
            case 'n': expect("null"); return null;
            default: return number();
        }
    }

    private Map<String, Object> object() {
        Map<String, Object> m = new LinkedHashMap<>();
        p++; // {
        ws();
        if (s.charAt(p) == '}') { p++; return m; }
        while (true) {
            ws();
            String k = string();
            ws();
            if (s.charAt(p) != ':') throw new IllegalArgumentException("expected ':' at " + p);
            p++;
            m.put(k, value());
            ws();
            char c = s.charAt(p);
            if (c == ',') { p++; continue; }
            if (c == '}') { p++; return m; }
            throw new IllegalArgumentException("expected ',' or '}' at " + p);
        }
    }

    private List<Object> array() {
        List<Object> l = new ArrayList<>();
        p++; // [
        ws();
        if (s.charAt(p) == ']') { p++; return l; }
        while (true) {
            l.add(value());
            ws();
            char c = s.charAt(p);
            if (c == ',') { p++; continue; }
            if (c == ']') { p++; return l; }
            throw new IllegalArgumentException("expected ',' or ']' at " + p);
        }
    }

    private String string() {
        if (s.charAt(p) != '"') {
            throw new IllegalArgumentException("expected string at " + p);
        }
        StringBuilder sb = new StringBuilder();
        p++;
        while (p < s.length()) {
            char c = s.charAt(p++);
            if (c == '"') return sb.toString();
            if (c == '\\') {
                if (p >= s.length()) throw new IllegalArgumentException("bad escape");
                char e = s.charAt(p++);
                switch (e) {
                    case '"': sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/': sb.append('/'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case 'n': sb.append('\n'); break;
                    case 'r': sb.append('\r'); break;
                    case 't': sb.append('\t'); break;
                    case 'u':
                        if (p + 4 > s.length()) throw new IllegalArgumentException("bad \\u");
                        sb.append((char) Integer.parseInt(s.substring(p, p + 4), 16));
                        p += 4;
                        break;
                    default: throw new IllegalArgumentException("bad escape: " + e);
                }
            } else {
                sb.append(c);
            }
        }
        throw new IllegalArgumentException("unterminated string");
    }

    private Object number() {
        int start = p;
        if (s.charAt(p) == '-') p++;
        while (p < s.length()) {
            char c = s.charAt(p);
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                p++;
            } else {
                break;
            }
        }
        String tok = s.substring(start, p);
        if (tok.isEmpty()) throw new IllegalArgumentException("bad number at " + start);
        if (tok.indexOf('.') < 0 && tok.indexOf('e') < 0 && tok.indexOf('E') < 0) {
            try {
                return Long.parseLong(tok);
            } catch (NumberFormatException nfe) {
                return Double.parseDouble(tok);
            }
        }
        return Double.parseDouble(tok);
    }

    private void expect(String lit) {
        if (!s.startsWith(lit, p)) {
            throw new IllegalArgumentException("expected " + lit + " at " + p);
        }
        p += lit.length();
    }

    private void ws() {
        while (p < s.length()) {
            char c = s.charAt(p);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p++;
            else return;
        }
    }

    // ---- serialization ----

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            sb.append(quote((String) v));
        } else if (v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Number) {
            sb.append(v);
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                sb.append(quote(e.getKey().toString())).append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object e : (Iterable<?>) v) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, e);
            }
            sb.append(']');
        } else {
            sb.append(quote(v.toString()));
        }
    }

    public static String quote(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.append('"').toString();
    }

    static Long toLong(Object o) {
        if (o == null) return null;
        if (o instanceof Number) return ((Number) o).longValue();
        return Long.parseLong(o.toString());
    }

    @SuppressWarnings("unchecked")
    static List<String> toStringList(Object o) {
        List<String> out = new ArrayList<>();
        if (o instanceof List) {
            for (Object e : (List<Object>) o) out.add((String) e);
        }
        return out;
    }
}
