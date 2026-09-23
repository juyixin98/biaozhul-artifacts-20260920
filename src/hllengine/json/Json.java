package hllengine.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A small, strict JSON parser and writer with no third-party dependencies.
 *
 * <p>Parsed values use only these Java types:
 * <ul>
 *   <li>object &rarr; {@link LinkedHashMap}&lt;String, Object&gt;</li>
 *   <li>array &rarr; {@link ArrayList}&lt;Object&gt;</li>
 *   <li>string &rarr; {@link String}</li>
 *   <li>integer (no fraction/exponent that loses precision) &rarr; {@link Long}</li>
 *   <li>number otherwise &rarr; {@link Double}</li>
 *   <li>true/false &rarr; {@link Boolean}</li>
 *   <li>null &rarr; Java {@code null}</li>
 * </ul>
 *
 * <p>The parser is strict on purpose (it is also a bad-format checker):
 * trailing commas, unquoted keys, single-quoted strings, comments, NaN /
 * Infinity, leading zeros and duplicate object keys are all rejected.
 */
public final class Json {

    private Json() {
    }

    // ---------------------------------------------------------------- parsing

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw p.error("trailing characters after JSON value");
        }
        return v;
    }

    /** Parses and requires a JSON object. */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected a JSON object at the top level, got " + typeName(v));
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return i >= s.length();
        }

        JsonException error(String msg) {
            int line = 1, col = 1;
            for (int j = 0; j < i && j < s.length(); j++) {
                if (s.charAt(j) == '\n') {
                    line++;
                    col = 1;
                } else {
                    col++;
                }
            }
            return new JsonException("invalid JSON: " + msg + " (line " + line + ", column " + col + ")");
        }

        void skipWs() {
            while (!eof()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    i++;
                } else {
                    return;
                }
            }
        }

        char peek() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            return s.charAt(i);
        }

        void expect(char c) {
            if (eof() || s.charAt(i) != c) {
                throw error("expected '" + c + "' but saw " + (eof() ? "end of input" : "'" + s.charAt(i) + "'"));
            }
            i++;
        }

        Object readValue() {
            skipWs();
            if (eof()) {
                throw error("expected a value but reached end of input");
            }
            char c = s.charAt(i);
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': return readLiteral("true", Boolean.TRUE);
                case 'f': return readLiteral("false", Boolean.FALSE);
                case 'n': return readLiteral("null", null);
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return readNumber();
                    }
                    throw error("unexpected character '" + c + "'");
            }
        }

        Object readLiteral(String literal, Object value) {
            if (!s.startsWith(literal, i)) {
                throw error("invalid literal, expected '" + literal + "'");
            }
            i += literal.length();
            return value;
        }

        Map<String, Object> readObject() {
            expect('{');
            LinkedHashMap<String, Object> out = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                i++;
                return out;
            }
            while (true) {
                skipWs();
                if (peek() != '"') {
                    throw error("object keys must be double-quoted strings");
                }
                String key = readString();
                if (out.containsKey(key)) {
                    throw error("duplicate object key \"" + key + "\"");
                }
                skipWs();
                expect(':');
                Object value = readValue();
                out.put(key, value);
                skipWs();
                char c = s.charAt(i++);
                if (c == '}') {
                    return out;
                }
                if (c != ',') {
                    throw error("expected ',' or '}' in object but saw '" + c + "'");
                }
                skipWs();
                if (peek() == '}') {
                    throw error("trailing comma in object");
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> out = new ArrayList<>();
            skipWs();
            if (peek() == ']') {
                i++;
                return out;
            }
            while (true) {
                out.add(readValue());
                skipWs();
                char c = s.charAt(i++);
                if (c == ']') {
                    return out;
                }
                if (c != ',') {
                    throw error("expected ',' or ']' in array but saw '" + c + "'");
                }
                skipWs();
                if (peek() == ']') {
                    throw error("trailing comma in array");
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw error("unterminated string");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c < 0x20) {
                    throw error("unescaped control character 0x" + Integer.toHexString(c) + " in string");
                }
                if (c == '\\') {
                    if (eof()) {
                        throw error("dangling escape in string");
                    }
                    char e = s.charAt(i++);
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
                            sb.append(readUnicodeEscape());
                            break;
                        default:
                            throw error("invalid escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        char readUnicodeEscape() {
            if (i + 4 > s.length()) {
                throw error("incomplete \\u escape");
            }
            int code = 0;
            for (int k = 0; k < 4; k++) {
                char h = s.charAt(i++);
                int d = Character.digit(h, 16);
                if (d < 0) {
                    throw error("invalid hex digit '" + h + "' in \\u escape");
                }
                code = (code << 4) | d;
            }
            return (char) code;
        }

        Object readNumber() {
            int start = i;
            boolean isDouble = false;
            if (peek() == '-') {
                i++;
                if (eof()) throw error("dangling minus sign");
            }
            char c = peek();
            if (c == '0') {
                i++;
                if (!eof()) {
                    char n = s.charAt(i);
                    if (n >= '0' && n <= '9') {
                        throw error("leading zeros are not allowed");
                    }
                }
            } else if (c >= '1' && c <= '9') {
                while (!eof() && s.charAt(i) >= '0' && s.charAt(i) <= '9') {
                    i++;
                }
            } else {
                throw error("invalid number");
            }
            if (!eof() && s.charAt(i) == '.') {
                isDouble = true;
                i++;
                if (eof() || s.charAt(i) < '0' || s.charAt(i) > '9') {
                    throw error("fraction part must contain at least one digit");
                }
                while (!eof() && s.charAt(i) >= '0' && s.charAt(i) <= '9') {
                    i++;
                }
            }
            if (!eof() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                isDouble = true;
                i++;
                if (!eof() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                    i++;
                }
                if (eof() || s.charAt(i) < '0' || s.charAt(i) > '9') {
                    throw error("exponent must contain at least one digit");
                }
                while (!eof() && s.charAt(i) >= '0' && s.charAt(i) <= '9') {
                    i++;
                }
            }
            String token = s.substring(start, i);
            if (isDouble) {
                double d = Double.parseDouble(token);
                if (Double.isNaN(d) || Double.isInfinite(d)) {
                    throw error("NaN and Infinity are not valid JSON numbers");
                }
                return d;
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException nfe) {
                // Integer magnitude beyond long: keep it as a double (rare for
                // this project; string-typed ids are recommended anyway).
                return Double.parseDouble(token);
            }
        }
    }

    // ---------------------------------------------------------------- writing

    /** Compact JSON encoding. */
    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    /** Human-readable JSON encoding with two-space indentation. */
    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v).booleanValue());
        } else if (v instanceof Long) {
            sb.append(v.toString());
        } else if (v instanceof Integer) {
            sb.append(((Integer) v).intValue());
        } else if (v instanceof Double) {
            double d = (Double) v;
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new JsonException("cannot encode NaN/Infinity as JSON");
            }
            sb.append(d);
        } else if (v instanceof Number) {
            sb.append(v.toString());
        } else if (v instanceof Map<?, ?>) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable<?>) {
            sb.append('[');
            boolean first = true;
            for (Object item : (Iterable<?>) v) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else if (v.getClass().isArray()) {
            sb.append('[');
            int n = java.lang.reflect.Array.getLength(v);
            for (int k = 0; k < n; k++) {
                if (k > 0) sb.append(',');
                writeTo(sb, java.lang.reflect.Array.get(v, k));
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void writePretty(StringBuilder sb, Object v, int depth) {
        if (v instanceof Map<?, ?> && !((Map<?, ?>) v).isEmpty()) {
            Map<?, ?> m = (Map<?, ?>) v;
            sb.append("{\n");
            int k = 0;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                indent(sb, depth + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), depth + 1);
                if (++k < m.size()) sb.append(',');
                sb.append('\n');
            }
            indent(sb, depth);
            sb.append('}');
        } else if (v instanceof Iterable<?>) {
            List<Object> items = new ArrayList<>();
            ((Iterable<?>) v).forEach(items::add);
            if (items.isEmpty()) {
                sb.append("[]");
            } else {
                sb.append("[\n");
                for (int k = 0; k < items.size(); k++) {
                    indent(sb, depth + 1);
                    writePretty(sb, items.get(k), depth + 1);
                    if (k < items.size() - 1) sb.append(',');
                    sb.append('\n');
                }
                indent(sb, depth);
                sb.append(']');
            }
        } else {
            writeTo(sb, v);
        }
    }

    private static void indent(StringBuilder sb, int depth) {
        for (int k = 0; k < depth * 2; k++) sb.append(' ');
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int k = 0; k < s.length(); k++) {
            char c = s.charAt(k);
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
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    // --------------------------------------------------------- typed accessors

    public static String typeName(Object v) {
        if (v == null) return "null";
        if (v instanceof Boolean) return "boolean";
        if (v instanceof String) return "string";
        if (v instanceof Long || v instanceof Integer) return "integer";
        if (v instanceof Number) return "number";
        if (v instanceof Map) return "object";
        if (v instanceof List) return "array";
        return v.getClass().getSimpleName();
    }

    public static Map<String, Object> asObject(Object v, String path) {
        if (!(v instanceof Map)) {
            throw new JsonException(path + " must be a JSON object, got " + typeName(v));
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) v;
        return m;
    }

    public static List<Object> asArray(Object v, String path) {
        if (!(v instanceof List)) {
            throw new JsonException(path + " must be a JSON array, got " + typeName(v));
        }
        @SuppressWarnings("unchecked")
        List<Object> l = (List<Object>) v;
        return l;
    }

    public static String asString(Object v, String path) {
        if (!(v instanceof String)) {
            throw new JsonException(path + " must be a string, got " + typeName(v));
        }
        return (String) v;
    }

    public static double asNumber(Object v, String path) {
        if (v instanceof Number) return ((Number) v).doubleValue();
        throw new JsonException(path + " must be a number, got " + typeName(v));
    }

    public static long asLong(Object v, String path) {
        if (v instanceof Long) return (Long) v;
        if (v instanceof Integer) return (Integer) v;
        throw new JsonException(path + " must be an integer, got " + typeName(v));
    }

    public static int asInt(Object v, String path, int min, int max) {
        long n = asLong(v, path);
        if (n < min || n > max) {
            throw new JsonException(path + " must be an integer in [" + min + ", " + max + "], got " + n);
        }
        return (int) n;
    }

    public static boolean asBool(Object v, String path) {
        if (!(v instanceof Boolean)) {
            throw new JsonException(path + " must be true or false, got " + typeName(v));
        }
        return (Boolean) v;
    }
}
