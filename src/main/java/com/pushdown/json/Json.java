package com.pushdown.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser/writer with no external dependencies.
 * Model: Map&lt;String,Object&gt;, List&lt;Object&gt;, String, Long, Double, Boolean, null.
 */
public final class Json {
    private Json() {}

    public static Object parse(String s) {
        Parser p = new Parser(s);
        p.ws();
        Object v = p.value();
        p.ws();
        if (!p.eof()) throw new IllegalArgumentException("trailing characters at offset " + p.pos);
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String s) {
        Object v = parse(s);
        if (!(v instanceof Map)) throw new IllegalArgumentException("expected a JSON object");
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }

        char peek() {
            if (eof()) throw new IllegalArgumentException("unexpected end of JSON");
            return s.charAt(pos);
        }

        void ws() { while (!eof() && Character.isWhitespace(s.charAt(pos))) pos++; }

        void expect(char c) {
            if (eof() || s.charAt(pos) != c)
                throw new IllegalArgumentException("expected '" + c + "' at offset " + pos);
            pos++;
        }

        Object value() {
            char c = peek();
            return switch (c) {
                case '{' -> object();
                case '[' -> array();
                case '"' -> string();
                case 't' -> { literal("true"); yield Boolean.TRUE; }
                case 'f' -> { literal("false"); yield Boolean.FALSE; }
                case 'n' -> { literal("null"); yield null; }
                default -> number();
            };
        }

        void literal(String lit) {
            if (!s.startsWith(lit, pos)) throw new IllegalArgumentException("bad literal at offset " + pos);
            pos += lit.length();
        }

        Map<String, Object> object() {
            expect('{'); ws();
            Map<String, Object> m = new LinkedHashMap<>();
            if (peek() == '}') { pos++; return m; }
            while (true) {
                ws();
                String k = string();
                ws(); expect(':'); ws();
                m.put(k, value());
                ws();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return m; }
                throw new IllegalArgumentException("expected ',' or '}' at offset " + pos);
            }
        }

        List<Object> array() {
            expect('['); ws();
            List<Object> l = new ArrayList<>();
            if (peek() == ']') { pos++; return l; }
            while (true) {
                ws();
                l.add(value());
                ws();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return l; }
                throw new IllegalArgumentException("expected ',' or ']' at offset " + pos);
            }
        }

        String string() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new IllegalArgumentException("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new IllegalArgumentException("unterminated escape");
                    char e = s.charAt(pos++);
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
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw new IllegalArgumentException("bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Number number() {
            int start = pos;
            if (!eof() && (s.charAt(pos) == '-' || s.charAt(pos) == '+')) pos++;
            boolean dbl = false;
            while (!eof()) {
                char c = s.charAt(pos);
                if (Character.isDigit(c)) pos++;
                else if (c == '.' || c == 'e' || c == 'E' || c == '-' || c == '+') { dbl = true; pos++; }
                else break;
            }
            if (start == pos) throw new IllegalArgumentException("bad value at offset " + pos);
            String t = s.substring(start, pos);
            try {
                return dbl ? (Number) Double.parseDouble(t) : Long.parseLong(t);
            } catch (NumberFormatException e) {
                return Double.parseDouble(t);
            }
        }
    }

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb);
        return sb.toString();
    }

    private static void write(Object v, StringBuilder sb) {
        if (v == null) sb.append("null");
        else if (v instanceof String str) { sb.append('"'); escape(str, sb); sb.append('"'); }
        else if (v instanceof Boolean || v instanceof Long || v instanceof Integer) sb.append(v);
        else if (v instanceof Double d) {
            if (!Double.isInfinite(d) && !Double.isNaN(d) && d == Math.floor(d)) sb.append(d.longValue()).append(".0");
            else sb.append(d);
        }
        else if (v instanceof Number) sb.append(v);
        else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                sb.append('"'); escape(String.valueOf(e.getKey()), sb); sb.append("\":");
                write(e.getValue(), sb);
            }
            sb.append('}');
        } else if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object o : it) {
                if (!first) sb.append(',');
                first = false;
                write(o, sb);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot write JSON for " + v.getClass());
        }
    }

    private static void escape(String s, StringBuilder sb) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
    }
}
