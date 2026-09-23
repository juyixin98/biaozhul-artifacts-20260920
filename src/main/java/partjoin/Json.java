package partjoin;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal, dependency-free JSON parser and serializer used for the request
 * entry point, plan/stats export and spill files.
 *
 * Parsed values map to Java types as:
 *   object -> LinkedHashMap, array -> ArrayList,
 *   string -> String, true/false -> Boolean, null -> null,
 *   integer fitting in long -> Long, larger integers -> BigInteger,
 *   floating-point literals -> BigDecimal (precision preserved).
 */
public final class Json {

    private Json() {
    }

    // ---------------- parsing ----------------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos < p.s.length()) throw p.error("Trailing characters");
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object o = parse(text);
        if (!(o instanceof Map)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Expected a JSON object");
        }
        return (Map<String, Object>) o;
    }

    public static Map<String, Object> getObject(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (o == null) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Missing '" + key + "' object");
        }
        if (!(o instanceof Map)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be an object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> typed = (Map<String, Object>) o;
        return typed;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> getArray(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (o == null) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Missing '" + key + "' array");
        }
        if (!(o instanceof List)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be an array");
        }
        return (List<Object>) o;
    }

    public static String getString(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (!(o instanceof String s)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be a string");
        }
        return s;
    }

    public static String optString(Map<String, Object> m, String key, String dflt) {
        Object o = m.get(key);
        return o == null ? dflt : o.toString();
    }

    public static int getInt(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (o instanceof Number n && o instanceof Long) return n.intValue();
        if (o instanceof Number n) return n.intValue();
        throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be an integer");
    }

    public static int optInt(Map<String, Object> m, String key, int dflt) {
        Object o = m.get(key);
        if (o == null) return dflt;
        if (!(o instanceof Number n)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be an integer");
        }
        return n.intValue();
    }

    public static long optLong(Map<String, Object> m, String key, long dflt) {
        Object o = m.get(key);
        if (o == null) return dflt;
        if (!(o instanceof Number n)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "'" + key + "' must be an integer");
        }
        return n.longValue();
    }

    public static List<String> getStringList(Map<String, Object> m, String key) {
        List<Object> arr = getArray(m, key);
        List<String> out = new ArrayList<>();
        for (Object o : arr) {
            if (!(o instanceof String s)) {
                throw new JoinException(JoinException.INVALID_REQUEST,
                        "'" + key + "' entries must be strings");
            }
            out.add(s);
        }
        return out;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        JoinException error(String msg) {
            return new JoinException(JoinException.INVALID_REQUEST,
                    "JSON parse error at position " + pos + ": " + msg);
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        Object readValue() {
            skipWs();
            if (pos >= s.length()) throw error("Unexpected end of input");
            char c = s.charAt(pos);
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
                String k = readString();
                skipWs();
                expect(':');
                Object v = readValue();
                m.put(k, v);
                skipWs();
                char c = next();
                if (c == '}') return m;
                if (c != ',') throw error("Expected ',' or '}'");
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
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ']') return list;
                if (c != ',') throw error("Expected ',' or ']'");
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= s.length()) throw error("Unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
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
                            if (pos + 4 > s.length()) throw error("Bad unicode escape");
                            int cp = Integer.parseInt(s.substring(pos, pos + 4), 16);
                            sb.append((char) cp);
                            pos += 4;
                        }
                        default -> throw error("Bad escape: \\" + e);
                    }
                } else {
                    if (c < 0x20) throw error("Unescaped control character in string");
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
            throw error("Invalid literal");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw error("Invalid literal");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) pos++;
            boolean floating = false;
            if (pos < s.length() && s.charAt(pos) == '.') {
                floating = true;
                pos++;
                while (pos < s.length() && Character.isDigit(s.charAt(pos))) pos++;
            }
            if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                floating = true;
                pos++;
                if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) pos++;
                while (pos < s.length() && Character.isDigit(s.charAt(pos))) pos++;
            }
            String lit = s.substring(start, pos);
            if (lit.isEmpty() || lit.equals("-")) throw error("Invalid number");
            if (floating) return new BigDecimal(lit);
            try {
                return Long.parseLong(lit);
            } catch (NumberFormatException e) {
                return new BigInteger(lit);
            }
        }

        char peek() {
            if (pos >= s.length()) throw error("Unexpected end of input");
            return s.charAt(pos);
        }

        char next() {
            if (pos >= s.length()) throw error("Unexpected end of input");
            return s.charAt(pos++);
        }

        void expect(char c) {
            if (pos >= s.length() || s.charAt(pos) != c) {
                throw error("Expected '" + c + "'");
            }
            pos++;
        }
    }

    // ---------------- serialization ----------------

    public static String write(Object o) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, o);
        return sb.toString();
    }

    public static String writePretty(Object o) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, o, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeIndented(StringBuilder sb, Object o, int depth) {
        String nl = "\n" + "  ".repeat(depth + 1);
        if (o instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                sb.append(nl);
                writeString(sb, e.getKey().toString());
                sb.append(": ");
                writeIndented(sb, e.getValue(), depth + 1);
            }
            sb.append('\n').append("  ".repeat(depth)).append('}');
        } else if (o instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) sb.append(',');
                first = false;
                sb.append(nl);
                writeIndented(sb, item, depth + 1);
            }
            sb.append('\n').append("  ".repeat(depth)).append(']');
        } else {
            writeValue(sb, o);
        }
    }

    @SuppressWarnings("rawtypes")
    private static void writeValue(StringBuilder sb, Object o) {
        if (o == null) {
            sb.append("null");
        } else if (o instanceof String s) {
            writeString(sb, s);
        } else if (o instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (o instanceof BigDecimal bd) {
            sb.append(bd.toPlainString());
        } else if (o instanceof BigInteger bi) {
            sb.append(bi.toString());
        } else if (o instanceof Number n) {
            sb.append(n.toString());
        } else if (o instanceof Map m) {
            sb.append('{');
            boolean first = true;
            for (Object eObj : m.entrySet()) {
                Map.Entry e = (Map.Entry) eObj;
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey().toString());
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (o instanceof List list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) sb.append(',');
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else if (o instanceof Enum<?> en) {
            writeString(sb, en.name());
        } else {
            writeString(sb, o.toString());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }
}
