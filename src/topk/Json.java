package topk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser/serializer.
 * Supports objects, arrays, strings, numbers (long/double), booleans, null.
 * Object key order is preserved (LinkedHashMap) so exported plans/data are stable.
 */
public final class Json {

    private Json() {}

    // ---------- parsing ----------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.parseValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw new JsonException("trailing characters at offset " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object at top level");
        }
        return (Map<String, Object>) v;
    }

    public static final class JsonException extends RuntimeException {
        public JsonException(String msg) { super(msg); }
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
                throw new JsonException("expected '" + c + "' at offset " + pos);
            }
            return s.charAt(pos++);
        }

        Object parseValue() {
            skipWs();
            if (atEnd()) throw new JsonException("unexpected end of input");
            char c = s.charAt(pos);
            switch (c) {
                case '{': return parseObject();
                case '[': return parseArray();
                case '"': return parseString();
                case 't': expectLiteral("true"); return Boolean.TRUE;
                case 'f': expectLiteral("false"); return Boolean.FALSE;
                case 'n': expectLiteral("null"); return null;
                default: return parseNumber();
            }
        }

        void expectLiteral(String lit) {
            if (!s.startsWith(lit, pos)) {
                throw new JsonException("invalid literal at offset " + pos);
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
                if (atEnd()) throw new JsonException("unterminated object");
                char c = s.charAt(pos++);
                if (c == ',') continue;
                if (c == '}') return map;
                throw new JsonException("expected ',' or '}' at offset " + (pos - 1));
            }
        }

        List<Object> parseArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (!atEnd() && s.charAt(pos) == ']') { pos++; return list; }
            while (true) {
                list.add(parseValue());
                skipWs();
                char c = s.charAt(pos++);
                if (c == ',') continue;
                if (c == ']') return list;
                throw new JsonException("expected ',' or ']' at offset " + (pos - 1));
            }
        }

        String parseString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) throw new JsonException("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (atEnd()) throw new JsonException("unterminated escape");
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
                            if (pos + 4 > s.length()) throw new JsonException("bad \\u escape");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default: throw new JsonException("bad escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Object parseNumber() {
            int start = pos;
            if (!atEnd() && s.charAt(pos) == '-') pos++;
            while (!atEnd() && Character.isDigit(s.charAt(pos))) pos++;
            boolean isDouble = false;
            if (!atEnd() && s.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                while (!atEnd() && Character.isDigit(s.charAt(pos))) pos++;
            }
            if (!atEnd() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (!atEnd() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) pos++;
                while (!atEnd() && Character.isDigit(s.charAt(pos))) pos++;
            }
            if (start == pos) {
                throw new JsonException("invalid value at offset " + start);
            }
            String num = s.substring(start, pos);
            try {
                return isDouble ? (Object) Double.parseDouble(num) : (Object) Long.parseLong(num);
            } catch (NumberFormatException e) {
                throw new JsonException("bad number '" + num + "'");
            }
        }
    }

    // ---------- serializing ----------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        write(value, sb);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void write(Object v, StringBuilder sb) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString((String) v, sb);
        } else if (v instanceof Boolean || v instanceof Integer || v instanceof Long) {
            sb.append(v.toString());
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (d == Math.floor(d) && !Double.isInfinite(d) && Math.abs(d) < 1e15) {
                sb.append(Long.toString((long) d));
            } else {
                sb.append(v.toString());
            }
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
            throw new JsonException("cannot serialize " + v.getClass());
        }
    }

    private static void writeString(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }

    // ---------- typed accessors for request maps ----------

    public static long getLong(Map<String, Object> m, String key, long def) {
        Object v = m.get(key);
        if (v == null) return def;
        if (v instanceof Number) return ((Number) v).longValue();
        throw new JsonException("field '" + key + "' must be a number");
    }

    public static String getString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) return null;
        if (v instanceof String) return (String) v;
        throw new JsonException("field '" + key + "' must be a string");
    }
}
