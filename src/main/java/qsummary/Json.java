package qsummary;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser and writer.
 *
 * <p>Parses objects ({@code LinkedHashMap<String,Object>}), arrays
 * ({@code ArrayList<Object>}), strings, numbers (long when integral,
 * double otherwise), booleans and null. This is intentionally small: the
 * service only exchanges its own well-defined document shapes.
 */
public final class Json {

    private final String s;
    private int p;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw new BadRequestException("request body is empty");
        }
        Json j = new Json(text);
        j.skipWs();
        Object v = j.readValue();
        j.skipWs();
        if (j.p != j.s.length()) {
            throw new BadRequestException("trailing characters after JSON value");
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (p >= s.length()) {
            throw new BadRequestException("unexpected end of JSON");
        }
        char c = s.charAt(p);
        switch (c) {
            case '{': return readObject();
            case '[': return readArray();
            case '"': return readString();
            case 't': case 'f': return readBool();
            case 'n': return readNull();
            default:  return readNumber();
        }
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') {
            p++;
            return map;
        }
        while (true) {
            skipWs();
            String key = readString();
            skipWs();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new BadRequestException("expected ',' or '}' in object");
            }
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        expect('[');
        skipWs();
        if (peek() == ']') {
            p++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new BadRequestException("expected ',' or ']' in array");
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            char c = next();
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char e = next();
                switch (e) {
                    case '"':  sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/':  sb.append('/'); break;
                    case 'b':  sb.append('\b'); break;
                    case 'f':  sb.append('\f'); break;
                    case 'n':  sb.append('\n'); break;
                    case 'r':  sb.append('\r'); break;
                    case 't':  sb.append('\t'); break;
                    case 'u':
                        int cp = 0;
                        for (int k = 0; k < 4; k++) {
                            char h = next();
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= h - '0';
                            else if (h >= 'a' && h <= 'f') cp |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') cp |= h - 'A' + 10;
                            else throw new BadRequestException("bad unicode escape");
                        }
                        sb.append((char) cp);
                        break;
                    default:
                        throw new BadRequestException("bad escape character");
                }
            } else {
                if (c < 0x20) {
                    throw new BadRequestException("unescaped control character in string");
                }
                sb.append(c);
            }
        }
    }

    private Boolean readBool() {
        if (s.startsWith("true", p)) {
            p += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", p)) {
            p += 5;
            return Boolean.FALSE;
        }
        throw new BadRequestException("invalid literal");
    }

    private Object readNull() {
        if (s.startsWith("null", p)) {
            p += 4;
            return null;
        }
        throw new BadRequestException("invalid literal");
    }

    private Number readNumber() {
        int start = p;
        if (peek() == '-') p++;
        while (p < s.length() && Character.isDigit(s.charAt(p))) p++;
        boolean floating = false;
        if (p < s.length() && s.charAt(p) == '.') {
            floating = true;
            p++;
            while (p < s.length() && Character.isDigit(s.charAt(p))) p++;
        }
        if (p < s.length() && (s.charAt(p) == 'e' || s.charAt(p) == 'E')) {
            floating = true;
            p++;
            if (p < s.length() && (s.charAt(p) == '+' || s.charAt(p) == '-')) p++;
            while (p < s.length() && Character.isDigit(s.charAt(p))) p++;
        }
        String token = s.substring(start, p);
        if (token.isEmpty() || "-".equals(token)) {
            throw new BadRequestException("invalid number");
        }
        try {
            if (!floating) {
                return Long.parseLong(token);
            }
            double d = Double.parseDouble(token);
            if (!Double.isFinite(d)) {
                throw new BadRequestException("non-finite number not allowed");
            }
            return d;
        } catch (NumberFormatException e) {
            throw new BadRequestException("invalid number: " + token);
        }
    }

    private void skipWs() {
        while (p < s.length() && Character.isWhitespace(s.charAt(p))) p++;
    }

    private char peek() {
        if (p >= s.length()) throw new BadRequestException("unexpected end of JSON");
        return s.charAt(p);
    }

    private char next() {
        if (p >= s.length()) throw new BadRequestException("unexpected end of JSON");
        return s.charAt(p++);
    }

    private void expect(char c) {
        char actual = next();
        if (actual != c) {
            throw new BadRequestException("expected '" + c + "' but got '" + actual + "'");
        }
    }

    // ------------------------------------------------------------------
    // Writer
    // ------------------------------------------------------------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String str) {
            writeString(sb, str);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            writeNumber(sb, n);
        } else if (value instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List<?> list) {
            writeArrayHeader(sb);
            boolean first = true;
            for (Object o : list) {
                if (!first) sb.append(',');
                first = false;
                writeValue(sb, o);
            }
            sb.append(']');
        } else if (value instanceof double[] ds) {
            writeArrayHeader(sb);
            for (int k = 0; k < ds.length; k++) {
                if (k > 0) sb.append(',');
                writeNumber(sb, ds[k]);
            }
            sb.append(']');
        } else if (value instanceof long[] ls) {
            writeArrayHeader(sb);
            for (int k = 0; k < ls.length; k++) {
                if (k > 0) sb.append(',');
                sb.append(ls[k]);
            }
            sb.append(']');
        } else if (value instanceof int[] is) {
            writeArrayHeader(sb);
            for (int k = 0; k < is.length; k++) {
                if (k > 0) sb.append(',');
                sb.append(is[k]);
            }
            sb.append(']');
        } else if (value instanceof boolean[] bs) {
            writeArrayHeader(sb);
            for (int k = 0; k < bs.length; k++) {
                if (k > 0) sb.append(',');
                sb.append(bs[k]);
            }
            sb.append(']');
        } else if (value instanceof Object[] os) {
            writeArrayHeader(sb);
            for (int k = 0; k < os.length; k++) {
                if (k > 0) sb.append(',');
                writeValue(sb, os[k]);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot serialize " + value.getClass());
        }
    }

    private static void writeArrayHeader(StringBuilder sb) {
        sb.append('[');
    }

    private static void writeNumber(StringBuilder sb, Number n) {
        if (n instanceof Double d) {
            if (!Double.isFinite(d.doubleValue())) {
                throw new IllegalArgumentException("non-finite double");
            }
            // Round-trip exact representation.
            sb.append(Double.toString(d));
        } else if (n instanceof Float f) {
            if (!Float.isFinite(f.floatValue())) {
                throw new IllegalArgumentException("non-finite float");
            }
            sb.append(Float.toString(f));
        } else {
            sb.append(n.toString());
        }
    }

    private static void writeString(StringBuilder sb, String str) {
        sb.append('"');
        for (int i = 0; i < str.length(); i++) {
            char c = str.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
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

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object o) {
        if (!(o instanceof Map<?, ?>)) {
            throw new BadRequestException("expected a JSON object");
        }
        return (Map<String, Object>) o;
    }

    public static String getString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (!(v instanceof String s)) {
            throw new BadRequestException("'" + key + "' must be a string");
        }
        return s;
    }

    public static double getDouble(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v instanceof Number n) {
            return n.doubleValue();
        }
        throw new BadRequestException("'" + key + "' must be a number");
    }
}
