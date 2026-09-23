package seqcep.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON value model backed by JDK collections only.
 *
 * <p>Types map as follows:
 * <ul>
 *   <li>object  -&gt; LinkedHashMap&lt;String, Object&gt;</li>
 *   <li>array   -&gt; java.util.List&lt;Object&gt;</li>
 *   <li>string  -&gt; String</li>
 *   <li>number  -&gt; Long when integral, Double otherwise</li>
 *   <li>true/false -&gt; Boolean</li>
 *   <li>null    -&gt; null (Json.NULL is an explicit marker for lookup defaults)</li>
 * </ul>
 */
public final class Json {
    private Json() {}

    /** Marker meaning "key absent"; callers distinguish it from an actual JSON null. */
    public static final Object MISSING = new Object();

    // ---------------------------------------------------------------- parser

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("Trailing characters at position " + p.pos);
        }
        return v;
    }

    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("Expected JSON object at top level");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) v;
        return m;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }
        char peek() { return s.charAt(pos); }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        Object readValue() {
            skipWs();
            if (eof()) throw new JsonException("Unexpected end of input");
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': case 'f': return readBoolean();
                case 'n': return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw new JsonException("Unexpected character '" + c + "' at position " + pos);
            }
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (!eof() && peek() == '}') { pos++; return map; }
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
                if (c != ',') throw new JsonException("Expected ',' or '}' at position " + (pos - 1));
            }
            return map;
        }

        List<Object> readArray() {
            List<Object> list = new java.util.ArrayList<>();
            expect('[');
            skipWs();
            if (!eof() && peek() == ']') { pos++; return list; }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ']') break;
                if (c != ',') throw new JsonException("Expected ',' or ']' at position " + (pos - 1));
            }
            return list;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new JsonException("Unterminated string");
                char c = next();
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new JsonException("Bad escape at end of input");
                    char e = next();
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
                            if (pos + 4 > s.length()) throw new JsonException("Bad \\u escape");
                            String hex = s.substring(pos, pos + 4);
                            pos += 4;
                            try { sb.append((char) Integer.parseInt(hex, 16)); }
                            catch (NumberFormatException ex) { throw new JsonException("Bad \\u escape: " + hex); }
                            break;
                        default: throw new JsonException("Invalid escape '\\" + e + "'");
                    }
                } else if (c < 0x20) {
                    throw new JsonException("Unescaped control character in string at position " + (pos - 1));
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw new JsonException("Invalid literal at position " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return null; }
            throw new JsonException("Invalid literal at position " + pos);
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            readDigits();
            boolean integral = true;
            if (!eof() && peek() == '.') {
                integral = false;
                pos++;
                readDigits();
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                integral = false;
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) pos++;
                readDigits();
            }
            String token = s.substring(start, pos);
            try {
                if (integral) return Long.parseLong(token);
                return Double.parseDouble(token);
            } catch (NumberFormatException ex) {
                throw new JsonException("Invalid number: " + token);
            }
        }

        void readDigits() {
            int start = pos;
            while (!eof() && Character.isDigit(peek())) pos++;
            if (pos == start) throw new JsonException("Expected digit at position " + pos);
        }

        char next() {
            if (eof()) throw new JsonException("Unexpected end of input");
            return s.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw new JsonException("Expected '" + c + "' but found '" + actual + "' at position " + (pos - 1));
            }
        }
    }

    // ---------------------------------------------------------------- writer

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> map = (Map<String, Object>) v;
            if (map.isEmpty()) { sb.append("{}"); return; }
            sb.append("{\n");
            int i = 0;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                pad(sb, indent + 1);
                writeString(sb, e.getKey());
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
                if (++i < map.size()) sb.append(',');
                sb.append('\n');
            }
            pad(sb, indent);
            sb.append('}');
        } else if (v instanceof List) {
            List<?> list = (List<?>) v;
            if (list.isEmpty()) { sb.append("[]"); return; }
            sb.append("[\n");
            for (int i = 0; i < list.size(); i++) {
                pad(sb, indent + 1);
                writePretty(sb, list.get(i), indent + 1);
                if (i < list.size() - 1) sb.append(',');
                sb.append('\n');
            }
            pad(sb, indent);
            sb.append(']');
        } else {
            writeValue(sb, v);
        }
    }

    private static void pad(StringBuilder sb, int level) {
        for (int i = 0; i < level; i++) sb.append("  ");
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Number) {
            Number n = (Number) v;
            if (n instanceof Double d) {
                if (d.isInfinite() || d.isNaN()) {
                    throw new JsonException("Non-finite double cannot be serialized");
                }
                sb.append(d);
            } else if (v instanceof Float f) {
                if (f.isInfinite() || f.isNaN()) {
                    throw new JsonException("Non-finite float cannot be serialized");
                }
                sb.append(f);
            } else {
                sb.append(n);
            }
        } else if (v instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> map = (Map<String, Object>) v;
            sb.append('{');
            int i = 0;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                if (i++ > 0) sb.append(',');
                writeString(sb, e.getKey());
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            List<?> list = (List<?>) v;
            sb.append('[');
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(',');
                writeValue(sb, list.get(i));
            }
            sb.append(']');
        } else {
            throw new JsonException("Cannot serialize value of type " + v.getClass().getName());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
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
        sb.append('"');
    }

    // ------------------------------------------------------------- helpers

    public static String getString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v instanceof String s) return s;
        return null;
    }

    public static String requireString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new JsonException("Field '" + key + "' must be a non-empty string");
        }
        return s;
    }

    public static long requireLong(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v instanceof Number n) return n.longValue();
        throw new JsonException("Field '" + key + "' must be a number (epoch milliseconds)");
    }

    public static long getLong(Map<String, Object> m, String key, long dflt) {
        Object v = m.get(key);
        if (v instanceof Number n) return n.longValue();
        return dflt;
    }

    public static Map<String, Object> obj(Object... kv) {
        if (kv.length % 2 != 0) throw new IllegalArgumentException("obj() needs key/value pairs");
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }
}
