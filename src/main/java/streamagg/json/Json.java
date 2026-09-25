package streamagg.json;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal, dependency-free JSON parser and writer.
 *
 * <p>Supports the full JSON grammar (objects, arrays, strings with escapes,
 * numbers parsed as {@link BigDecimal}, true/false/null). This deliberately
 * avoids pulling in Jackson/Gson so the project builds with a plain JDK.
 */
public final class Json {

    private Json() {
    }

    // ---------------------------------------------------------------------
    // Typed view over a JSON value
    // ---------------------------------------------------------------------

    public static final class JsonValue {
        public final Object value;

        JsonValue(Object value) {
            this.value = value;
        }

        public boolean isObject() {
            return value instanceof Map;
        }

        @SuppressWarnings("unchecked")
        public Map<String, JsonValue> asObject() {
            return (Map<String, JsonValue>) value;
        }

        public boolean isArray() {
            return value instanceof List;
        }

        @SuppressWarnings("unchecked")
        public List<JsonValue> asArray() {
            return (List<JsonValue>) value;
        }

        public boolean isString() {
            return value instanceof String;
        }

        public String asString() {
            return (String) value;
        }

        public boolean isNumber() {
            return value instanceof BigDecimal;
        }

        public BigDecimal asBigDecimal() {
            return (BigDecimal) value;
        }

        public long asLong() {
            return asBigDecimal().longValueExact();
        }

        public boolean isBoolean() {
            return value instanceof Boolean;
        }

        public boolean asBoolean() {
            return (Boolean) value;
        }

        public boolean isNull() {
            return value == null;
        }

        public JsonValue get(String key) {
            return asObject().get(key);
        }

        /** Object property as string, or null when absent/not a string. */
        public String stringOr(String key) {
            JsonValue v = asObject().get(key);
            return v != null && v.isString() ? v.asString() : null;
        }

        public BigDecimal decimalOr(String key) {
            JsonValue v = asObject().get(key);
            return v != null && v.isNumber() ? v.asBigDecimal() : null;
        }

        public Long longOr(String key) {
            JsonValue v = asObject().get(key);
            return v != null && v.isNumber() ? v.asBigDecimal().longValueExact() : null;
        }
    }

    public static JsonValue wrap(Object o) {
        return new JsonValue(o);
    }

    // ---------------------------------------------------------------------
    // Parsing
    // ---------------------------------------------------------------------

    public static JsonValue parse(String input) {
        Parser p = new Parser(input);
        JsonValue v = p.readValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw p.error("trailing characters after JSON value");
        }
        return v;
    }

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) {
            this.s = s;
        }

        boolean atEnd() {
            return i >= s.length();
        }

        JsonException error(String msg) {
            return new JsonException("JSON parse error at position " + i + ": " + msg);
        }

        void skipWs() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    i++;
                } else {
                    break;
                }
            }
        }

        char next() {
            if (atEnd()) {
                throw error("unexpected end of input");
            }
            return s.charAt(i++);
        }

        char expect(char c) {
            char a = next();
            if (a != c) {
                throw error("expected '" + c + "' but found '" + a + "'");
            }
            return a;
        }

        JsonValue readValue() {
            skipWs();
            char c = next();
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> new JsonValue(readStringAfterQuote());
                case 't' -> readLiteral("true", Boolean.TRUE);
                case 'f' -> readLiteral("false", Boolean.FALSE);
                case 'n' -> readLiteral("null", null);
                default -> {
                    if (c == '-' || c >= '0' && c <= '9') {
                        i--;
                        yield readNumber();
                    }
                    throw error("unexpected character '" + c + "'");
                }
            };
        }

        JsonValue readLiteral(String literal, Object value) {
            // caller already consumed the first char
            for (int k = 1; k < literal.length(); k++) {
                if (atEnd() || s.charAt(i) != literal.charAt(k)) {
                    throw error("invalid literal, expected '" + literal + "'");
                }
                i++;
            }
            return new JsonValue(value);
        }

        JsonValue readObject() {
            Map<String, JsonValue> map = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                i++;
                return new JsonValue(map);
            }
            while (true) {
                skipWs();
                if (next() != '"') {
                    throw error("expected string key in object");
                }
                String key = readStringAfterQuote();
                skipWs();
                expect(':');
                JsonValue v = readValue();
                map.put(key, v);
                skipWs();
                char c = next();
                if (c == '}') {
                    break;
                }
                if (c != ',') {
                    throw error("expected ',' or '}' in object, found '" + c + "'");
                }
            }
            return new JsonValue(map);
        }

        JsonValue readArray() {
            List<JsonValue> list = new ArrayList<>();
            skipWs();
            if (peek() == ']') {
                i++;
                return new JsonValue(list);
            }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ']') {
                    break;
                }
                if (c != ',') {
                    throw error("expected ',' or ']' in array, found '" + c + "'");
                }
            }
            return new JsonValue(list);
        }

        char peek() {
            return atEnd() ? '\0' : s.charAt(i);
        }

        JsonValue readNumber() {
            int start = i;
            if (peek() == '-') {
                i++;
            }
            if (peek() == '0') {
                i++;
            } else if (peek() >= '1' && peek() <= '9') {
                while (!atEnd() && Character.isDigit(peek())) {
                    i++;
                }
            } else {
                throw error("invalid number");
            }
            if (!atEnd() && peek() == '.') {
                i++;
                if (atEnd() || !Character.isDigit(peek())) {
                    throw error("expected digit after decimal point");
                }
                while (!atEnd() && Character.isDigit(peek())) {
                    i++;
                }
            }
            if (!atEnd() && (peek() == 'e' || peek() == 'E')) {
                i++;
                if (!atEnd() && (peek() == '+' || peek() == '-')) {
                    i++;
                }
                if (atEnd() || !Character.isDigit(peek())) {
                    throw error("expected digit in exponent");
                }
                while (!atEnd() && Character.isDigit(peek())) {
                    i++;
                }
            }
            String num = s.substring(start, i);
            return new JsonValue(new BigDecimal(num));
        }

        /** Position is right after the opening quote. */
        String readStringAfterQuote() {
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) {
                    throw error("unterminated string");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c < 0x20) {
                    throw error("unescaped control character in string");
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
                            if (i + 4 > s.length()) {
                                throw error("bad unicode escape");
                            }
                            String hex = s.substring(i, i + 4);
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException nfe) {
                                throw error("bad unicode escape: " + hex);
                            }
                            i += 4;
                        }
                        default -> throw error("invalid escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
        }
    }

    // ---------------------------------------------------------------------
    // Writing
    // ---------------------------------------------------------------------

    /** Compact JSON. */
    public static String write(JsonValue value) {
        return write(value, false);
    }

    /** Human-readable JSON with 2-space indentation. */
    public static String pretty(JsonValue value) {
        return write(value, true);
    }

    @SuppressWarnings("unchecked")
    private static String write(JsonValue value, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value.value, pretty, 0);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object o, boolean pretty, int depth) {
        if (o instanceof JsonValue jv) {
            o = jv.value; // unwrap nested parsed values
        }
        if (o == null) {
            sb.append("null");
        } else if (o instanceof String str) {
            writeString(sb, str);
        } else if (o instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (o instanceof BigDecimal bd) {
            String t = bd.stripTrailingZeros().toPlainString();
            sb.append(t);
        } else if (o instanceof Number n) {
            sb.append(n.toString());
        } else if (o instanceof Map<?, ?> map) {
            writeObject(sb, (Map<String, Object>) map, pretty, depth);
        } else if (o instanceof List<?> list) {
            writeArray(sb, (List<Object>) list, pretty, depth);
        } else {
            throw new JsonException("cannot serialize value of type " + o.getClass());
        }
    }

    private static void writeObject(StringBuilder sb, Map<String, Object> map, boolean pretty, int depth) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        int n = 0;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (n++ > 0) {
                sb.append(',');
            }
            newline(sb, pretty, depth + 1);
            writeString(sb, e.getKey());
            sb.append(pretty ? ": " : ":");
            writeValue(sb, e.getValue(), pretty, depth + 1);
        }
        newline(sb, pretty, depth);
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<Object> list, boolean pretty, int depth) {
        if (list.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        for (int k = 0; k < list.size(); k++) {
            if (k > 0) {
                sb.append(',');
            }
            newline(sb, pretty, depth + 1);
            writeValue(sb, list.get(k), pretty, depth + 1);
        }
        newline(sb, pretty, depth);
        sb.append(']');
    }

    private static void newline(StringBuilder sb, boolean pretty, int depth) {
        if (pretty) {
            sb.append('\n');
            sb.append("  ".repeat(depth));
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
