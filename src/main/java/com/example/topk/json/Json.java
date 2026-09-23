package com.example.topk.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser/writer.
 * Supports objects, arrays, strings (with escapes), numbers, booleans and null.
 * Objects parse to LinkedHashMap (insertion order preserved), arrays to List,
 * integers to Long, floating point to Double.
 */
public final class Json {
    private Json() {}

    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object v = p.parseValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw new IllegalArgumentException("trailing characters in JSON at offset " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean atEnd() {
            return pos >= s.length();
        }

        void skipWs() {
            while (!atEnd() && " \t\n\r".indexOf(s.charAt(pos)) >= 0) pos++;
        }

        char peek() {
            if (atEnd()) throw new IllegalArgumentException("unexpected end of JSON");
            return s.charAt(pos);
        }

        Object parseValue() {
            skipWs();
            char c = peek();
            switch (c) {
                case '{': return parseObject();
                case '[': return parseArray();
                case '"': return parseString();
                case 't': expect("true"); return Boolean.TRUE;
                case 'f': expect("false"); return Boolean.FALSE;
                case 'n': expect("null"); return null;
                default: return parseNumber();
            }
        }

        void expect(String lit) {
            if (!s.startsWith(lit, pos)) {
                throw new IllegalArgumentException("expected '" + lit + "' at offset " + pos);
            }
            pos += lit.length();
        }

        Map<String, Object> parseObject() {
            Map<String, Object> m = new LinkedHashMap<>();
            pos++; // consume '{'
            skipWs();
            if (peek() == '}') { pos++; return m; }
            while (true) {
                skipWs();
                String key = parseString();
                skipWs();
                if (peek() != ':') throw new IllegalArgumentException("expected ':' at offset " + pos);
                pos++;
                m.put(key, parseValue());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return m; }
                throw new IllegalArgumentException("expected ',' or '}' at offset " + pos);
            }
        }

        List<Object> parseArray() {
            List<Object> l = new ArrayList<>();
            pos++; // consume '['
            skipWs();
            if (peek() == ']') { pos++; return l; }
            while (true) {
                l.add(parseValue());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return l; }
                throw new IllegalArgumentException("expected ',' or ']' at offset " + pos);
            }
        }

        String parseString() {
            if (peek() != '"') throw new IllegalArgumentException("expected string at offset " + pos);
            pos++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) throw new IllegalArgumentException("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (atEnd()) throw new IllegalArgumentException("unterminated escape");
                    char e = s.charAt(pos++);
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
                            if (pos + 4 > s.length()) throw new IllegalArgumentException("bad \\u escape");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default: throw new IllegalArgumentException("bad escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Number parseNumber() {
            int start = pos;
            if (!atEnd() && s.charAt(pos) == '-') pos++;
            boolean floating = false;
            while (!atEnd()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '-' || c == '+') {
                    floating = true;
                    pos++;
                } else {
                    break;
                }
            }
            if (start == pos) throw new IllegalArgumentException("expected value at offset " + start);
            String num = s.substring(start, pos);
            try {
                return floating ? (Number) Double.valueOf(num) : (Number) Long.valueOf(num);
            } catch (NumberFormatException e) {
                return Double.valueOf(num);
            }
        }
    }

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb);
        return sb.toString();
    }

    private static void write(Object v, StringBuilder sb) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String str) { writeString(str, sb); return; }
        if (v instanceof Boolean || v instanceof Integer || v instanceof Long) { sb.append(v); return; }
        if (v instanceof Number num) {
            double d = num.doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("non-finite number is not representable in JSON");
            }
            sb.append(num);
            return;
        }
        if (v instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(String.valueOf(e.getKey()), sb);
                sb.append(':');
                write(e.getValue(), sb);
            }
            sb.append('}');
            return;
        }
        if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object o : it) {
                if (!first) sb.append(',');
                first = false;
                write(o, sb);
            }
            sb.append(']');
            return;
        }
        throw new IllegalArgumentException("cannot serialize value of type " + v.getClass());
    }

    private static void writeString(String str, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < str.length(); i++) {
            char c = str.charAt(i);
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
        sb.append('"');
    }
}
