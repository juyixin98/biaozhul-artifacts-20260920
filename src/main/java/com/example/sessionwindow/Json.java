package com.example.sessionwindow;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal hand-written JSON parser / writer.
 * Supports Map (LinkedHashMap), List, String, Long, Double, Boolean, null.
 * Numbers without fraction/exponent are parsed as Long.
 */
public final class Json {

    private Json() {
    }

    // ---------------- writer ----------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object item : it) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else if (v instanceof Object[] arr) {
            writeValue(sb, List.of(arr));
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("Non-finite double cannot be serialized");
            }
            sb.append(v);
        } else {
            // Long, Integer, BigInteger, etc.
            sb.append(v);
        }
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (v instanceof Iterable<?> it && !isScalarCollection(it)) {
            boolean first = true;
            boolean any = false;
            for (Object item : it) {
                any = true;
                break;
            }
            if (!any) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            for (Object item : it) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writePretty(sb, item, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeValue(sb, v);
        }
    }

    private static boolean isScalarCollection(Iterable<?> it) {
        for (Object item : it) {
            if (item instanceof Map || item instanceof Iterable) {
                return false;
            }
        }
        return true;
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

    // ---------------- parser ----------------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new IllegalArgumentException("Trailing content at position " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("Expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        char peek() {
            return s.charAt(pos);
        }

        void skipWs() {
            while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
                pos++;
            }
        }

        Object readValue() {
            if (eof()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            char c = peek();
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
            Map<String, Object> m = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (peek() == '}') {
                pos++;
                return m;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object value = readValue();
                m.put(key, value);
                skipWs();
                char c = next();
                if (c == '}') {
                    return m;
                }
                if (c != ',') {
                    throw new IllegalArgumentException("Expected ',' or '}' at position " + pos);
                }
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (peek() == ']') {
                pos++;
                return list;
            }
            while (true) {
                Object value = readValue();
                list.add(value);
                skipWs();
                char c = next();
                if (c == ']') {
                    return list;
                }
                if (c != ',') {
                    throw new IllegalArgumentException("Expected ',' or ']' at position " + pos);
                }
                skipWs();
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new IllegalArgumentException("Unterminated string");
                }
                char c = next();
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    char e = next();
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'u' -> {
                            if (pos + 4 > s.length()) {
                                throw new IllegalArgumentException("Bad unicode escape");
                            }
                            int code = Integer.parseInt(s.substring(pos, pos + 4), 16);
                            pos += 4;
                            sb.append((char) code);
                        }
                        default -> throw new IllegalArgumentException("Bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new IllegalArgumentException("Invalid literal at position " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new IllegalArgumentException("Invalid literal at position " + pos);
        }

        Number readNumber() {
            int start = pos;
            boolean isDouble = false;
            if (peek() == '-') {
                pos++;
            }
            while (!eof()) {
                char c = peek();
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    isDouble = true;
                    pos++;
                } else {
                    break;
                }
            }
            String token = s.substring(start, pos);
            if (token.isEmpty() || "-".equals(token)) {
                throw new IllegalArgumentException("Invalid number at position " + start);
            }
            return isDouble ? Double.parseDouble(token) : Long.parseLong(token);
        }

        char next() {
            if (eof()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw new IllegalArgumentException("Expected '" + c + "' but got '" + actual + "' at position " + (pos - 1));
            }
        }
    }

    // ---------------- typed accessors ----------------

    public static String str(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof String s) {
            return s;
        }
        throw new IllegalArgumentException("Field '" + key + "' must be a string");
    }

    public static String requireStr(Map<String, Object> m, String key) {
        String v = str(m, key);
        if (v == null || v.isEmpty()) {
            throw new IllegalArgumentException("Field '" + key + "' is required and must be a non-empty string");
        }
        return v;
    }

    public static long num(Map<String, Object> m, String key, long dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new IllegalArgumentException("Field '" + key + "' must be a number");
    }

    public static Long numOrNull(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new IllegalArgumentException("Field '" + key + "' must be a number");
    }
}
