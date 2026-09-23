package approx.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal recursive-descent JSON parser with no external dependencies.
 *
 * <p>Supported types: object ({@code LinkedHashMap}), array ({@code
 * ArrayList}), string ({@code String}), number ({@code Long} or {@code
 * Double}), true/false ({@code Boolean}), null ({@code null}). Sufficient for
 * the event service's request/response bodies; not a general-purpose library.
 */
public final class Json {

    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw new JsonException("empty JSON body");
        }
        Json p = new Json(text);
        p.skipWhitespace();
        Object value = p.readValue();
        p.skipWhitespace();
        if (p.pos != p.src.length()) {
            throw new JsonException("trailing characters at position " + p.pos);
        }
        return value;
    }

    private Object readValue() {
        skipWhitespace();
        if (pos >= src.length()) {
            throw new JsonException("unexpected end of JSON");
        }
        char c = src.charAt(pos);
        switch (c) {
            case '{': return readObject();
            case '[': return readArray();
            case '"': return readString();
            case 't': case 'f': return readBoolean();
            case 'n': return readNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) {
                    return readNumber();
                }
                throw new JsonException("unexpected character '" + c + "' at position " + pos);
        }
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWhitespace();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWhitespace();
            String key = readString();
            skipWhitespace();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWhitespace();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at position " + (pos - 1));
            }
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        expect('[');
        skipWhitespace();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWhitespace();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or ']' at position " + (pos - 1));
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new JsonException("unterminated string");
            }
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c < 0x20) {
                throw new JsonException("unescaped control character in string at position " + (pos - 1));
            }
            if (c == '\\') {
                if (pos >= src.length()) {
                    throw new JsonException("unterminated escape");
                }
                char e = src.charAt(pos++);
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
                        if (pos + 4 > src.length()) {
                            throw new JsonException("bad \\u escape at position " + pos);
                        }
                        String hex = src.substring(pos, pos + 4);
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException nfe) {
                            throw new JsonException("bad \\u escape: " + hex);
                        }
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("invalid escape '\\" + e + "' at position " + (pos - 1));
                }
            } else {
                sb.append(c);
            }
        }
    }

    private Boolean readBoolean() {
        if (src.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (src.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("invalid literal at position " + pos);
    }

    private Object readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonException("invalid literal at position " + pos);
    }

    private Number readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        // Integer part: reject leading zeros ("01") per RFC 8259.
        int intStart = pos;
        readDigits();
        if (pos - intStart > 1 && src.charAt(intStart) == '0') {
            throw new JsonException("leading zeros are not allowed in numbers at position " + start);
        }
        boolean isFloating = false;
        if (pos < src.length() && src.charAt(pos) == '.') {
            isFloating = true;
            pos++;
            readDigits();
        }
        if (pos < src.length() && (src.charAt(pos) == 'e' || src.charAt(pos) == 'E')) {
            isFloating = true;
            pos++;
            if (pos < src.length() && (src.charAt(pos) == '+' || src.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        String token = src.substring(start, pos);
        try {
            // NB: a ternary across double/long would binary-numeric-promote BOTH
            // branches to double (JLS 15.25), boxing integers as Double. Keep an
            // explicit if/else so integral tokens come back as Long.
            if (isFloating) {
                return Double.parseDouble(token);
            }
            return Long.parseLong(token);
        } catch (NumberFormatException nfe) {
            throw new JsonException("invalid number: " + token);
        }
    }

    private void readDigits() {
        int start = pos;
        while (pos < src.length() && src.charAt(pos) >= '0' && src.charAt(pos) <= '9') {
            pos++;
        }
        if (start == pos) {
            throw new JsonException("expected digits at position " + pos);
        }
    }

    private void skipWhitespace() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }

    private char peek() {
        return pos >= src.length() ? '\0' : src.charAt(pos);
    }

    private char next() {
        if (pos >= src.length()) {
            throw new JsonException("unexpected end of JSON");
        }
        return src.charAt(pos++);
    }

    private void expect(char c) {
        if (pos >= src.length() || src.charAt(pos) != c) {
            throw new JsonException("expected '" + c + "' at position " + pos);
        }
        pos++;
    }

    // ---------------------------------------------------------------- serialization

    /** Serialize a Java value (the types produced by {@link #parse}) to JSON text. */
    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Integer || value instanceof Long) {
            sb.append(value);
        } else if (value instanceof Number) {
            double d = ((Number) value).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new JsonException("cannot serialize non-finite number");
            }
            long asLong = (long) d;
            // Explicit if/else: a ternary here would binary-promote the long branch
            // to double and emit integral values as scientific notation (JLS 15.25).
            if (asLong == d && Math.abs(d) < 9.0e15) {
                sb.append(asLong);
            } else {
                sb.append(d);
            }
        } else if (value instanceof Map<?, ?>) {
            writeObject(sb, (Map<?, ?>) value);
        } else if (value instanceof List<?>) {
            writeArray(sb, (List<?>) value);
        } else {
            throw new JsonException("cannot serialize value of type " + value.getClass().getName());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(':');
            writeValue(sb, e.getValue());
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<?> list) {
        sb.append('[');
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            writeValue(sb, list.get(i));
        }
        sb.append(']');
    }

    private static void writeString(StringBuilder sb, String s) {
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
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }
}
