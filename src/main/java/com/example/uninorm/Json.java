package com.example.uninorm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser/encoder (RFC 8259 subset: objects, arrays, strings with
 * escapes including unicode escapes and surrogate pairs, numbers, booleans, null).
 * Written by hand so the project has zero external dependencies.
 */
public final class Json {

    private Json() {}

    // ---------------- parsing ----------------

    public static Object parse(String s) {
        Parser p = new Parser(s);
        p.skipWs();
        Object v = p.parseValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw new IllegalArgumentException("trailing characters at offset " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String s) {
        Object v = parse(s);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        boolean atEnd() { return pos >= s.length(); }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        char expect(char c) {
            if (atEnd() || s.charAt(pos) != c) {
                throw new IllegalArgumentException(
                        "expected '" + c + "' at offset " + pos);
            }
            return s.charAt(pos++);
        }

        Object parseValue() {
            skipWs();
            if (atEnd()) throw new IllegalArgumentException("unexpected end of input");
            char c = s.charAt(pos);
            switch (c) {
                case '{': return parseObject();
                case '[': return parseArray();
                case '"': return parseString();
                case 't': expectLit("true"); return Boolean.TRUE;
                case 'f': expectLit("false"); return Boolean.FALSE;
                case 'n': expectLit("null"); return null;
                default: return parseNumber();
            }
        }

        void expectLit(String lit) {
            if (!s.startsWith(lit, pos)) {
                throw new IllegalArgumentException("bad literal at offset " + pos);
            }
            pos += lit.length();
        }

        Map<String, Object> parseObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWs();
            if (!atEnd() && s.charAt(pos) == '}') { pos++; return map; }
            while (true) {
                skipWs();
                String key = parseString();
                skipWs();
                expect(':');
                Object val = parseValue();
                map.put(key, val);
                skipWs();
                char c = expect(s.charAt(pos)); // ',' or '}'
                if (c == '}') break;
                if (c != ',') throw new IllegalArgumentException("expected ',' or '}' at offset " + (pos - 1));
            }
            return map;
        }

        List<Object> parseArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (!atEnd() && s.charAt(pos) == ']') { pos++; return list; }
            while (true) {
                list.add(parseValue());
                skipWs();
                char c = expect(s.charAt(pos));
                if (c == ']') break;
                if (c != ',') throw new IllegalArgumentException("expected ',' or ']' at offset " + (pos - 1));
            }
            return list;
        }

        String parseString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) throw new IllegalArgumentException("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') break;
                if (c == '\\') {
                    if (atEnd()) throw new IllegalArgumentException("bad escape at end");
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
                        default: throw new IllegalArgumentException("bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Number parseNumber() {
            int start = pos;
            if (!atEnd() && (s.charAt(pos) == '-' || s.charAt(pos) == '+')) pos++;
            boolean frac = false, exp = false;
            while (!atEnd()) {
                char c = s.charAt(pos);
                if (Character.isDigit(c)) pos++;
                else if (c == '.' && !frac && !exp) { frac = true; pos++; }
                else if ((c == 'e' || c == 'E') && !exp) {
                    exp = true; pos++;
                    if (!atEnd() && (s.charAt(pos) == '-' || s.charAt(pos) == '+')) pos++;
                } else break;
            }
            if (start == pos) throw new IllegalArgumentException("bad value at offset " + start);
            String num = s.substring(start, pos);
            if (frac || exp) return Double.parseDouble(num);
            try {
                return Long.parseLong(num);
            } catch (NumberFormatException e) {
                return Double.parseDouble(num);
            }
        }
    }

    // ---------------- encoding ----------------

    public static String encode(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void write(Object v, StringBuilder sb) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString((String) v, sb);
        } else if (v instanceof Number || v instanceof Boolean) {
            sb.append(v.toString());
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(e.getKey(), sb);
                sb.append(':');
                write(e.getValue(), sb);
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object o : (Iterable<Object>) v) {
                if (!first) sb.append(',');
                first = false;
                write(o, sb);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot encode " + v.getClass());
        }
    }

    private static void writeString(String s, StringBuilder sb) {
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
                        sb.append(c); // UTF-8 transport handles the rest
                    }
            }
        }
        sb.append('"');
    }

    /** Convenience object builder: obj("a", 1, "b", "x"). */
    public static Map<String, Object> obj(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }
}
