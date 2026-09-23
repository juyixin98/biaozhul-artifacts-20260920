package com.example.vecsearch;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser / writer implemented on top of the JDK only.
 *
 * Supported values: Map<String,Object> (objects, insertion ordered),
 * List<Object> (arrays), String, Double, Boolean, null.
 * Numbers are parsed as double (all vector coordinates are doubles here).
 *
 * This is intentionally compact: the service speaks JSON but has no third
 * party dependencies by requirement.
 */
public final class Json {

    private Json() {}

    // ------------------------------------------------------------------
    // Parsing
    // ------------------------------------------------------------------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.src.length()) {
            throw new JsonException("Trailing characters at position " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("Expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    public static final class JsonException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public JsonException(String msg) { super(msg); }
    }

    private static final class Parser {
        final String src;
        int pos;

        Parser(String src) { this.src = src; }

        void skipWs() {
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object readValue() {
            skipWs();
            if (pos >= src.length()) {
                throw new JsonException("Unexpected end of input");
            }
            char c = src.charAt(pos);
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> readNumber();
            };
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (peek() == '}') { pos++; return map; }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object value = readValue();
                map.put(key, value);
                skipWs();
                char c = next();
                if (c == '}') break;
                if (c != ',') throw new JsonException("Expected ',' or '}' at " + pos);
            }
            return map;
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (peek() == ']') { pos++; return list; }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ']') break;
                if (c != ',') throw new JsonException("Expected ',' or ']' at " + pos);
            }
            return list;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= src.length()) {
                    throw new JsonException("Unterminated string");
                }
                char c = src.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    char e = next();
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'u' -> {
                            if (pos + 4 > src.length()) {
                                throw new JsonException("Bad unicode escape");
                            }
                            sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw new JsonException("Bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (src.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (src.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw new JsonException("Invalid literal at " + pos);
        }

        Object readNull() {
            if (src.startsWith("null", pos)) { pos += 4; return null; }
            throw new JsonException("Invalid literal at " + pos);
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E'
                        || c == '+' || c == '-') {
                    pos++;
                } else {
                    break;
                }
            }
            if (start == pos || (pos == start + 1 && src.charAt(start) == '-')) {
                throw new JsonException("Invalid number at " + start);
            }
            return Double.valueOf(src.substring(start, pos));
        }

        char peek() {
            if (pos >= src.length()) throw new JsonException("Unexpected end of input");
            return src.charAt(pos);
        }

        char next() {
            if (pos >= src.length()) throw new JsonException("Unexpected end of input");
            return src.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw new JsonException("Expected '" + c + "' but got '" + actual + "' at " + pos);
            }
        }
    }

    // ------------------------------------------------------------------
    // Writing
    // ------------------------------------------------------------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            writeNumber(sb, n.doubleValue());
        } else if (value instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else if (value instanceof Object[] arr) {
            sb.append('[');
            for (int i = 0; i < arr.length; i++) {
                if (i > 0) sb.append(',');
                writeTo(sb, arr[i]);
            }
            sb.append(']');
        } else if (value instanceof double[] arr) {
            sb.append('[');
            for (int i = 0; i < arr.length; i++) {
                if (i > 0) sb.append(',');
                writeNumber(sb, arr[i]);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(value));
        }
    }

    private static void writeNumber(StringBuilder sb, double d) {
        if (Double.isNaN(d) || Double.isInfinite(d)) {
            // JSON has no NaN/Infinity; clamp to null-like behavior is unsafe,
            // distances in this service are always finite, so this signals a bug.
            throw new JsonException("Cannot serialize non-finite number");
        }
        if (d == Math.rint(d) && !Double.isInfinite(d)
                && Math.abs(d) < 1e16) {
            // Render integral doubles without a trailing ".0" for readability.
            long l = (long) d;
            sb.append(l);
        } else {
            sb.append(d);
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
