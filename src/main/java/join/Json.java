package join;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal recursive-descent JSON parser / serializer.
 *
 * Supported Java mapping:
 *   object -> LinkedHashMap&lt;String,Object&gt;
 *   array  -> ArrayList&lt;Object&gt;
 *   string -> String
 *   integer number (no fraction/exponent, fits in long) -> Long
 *   other number -> Double
 *   true/false -> Boolean, null -> null
 *
 * This is deliberately tiny: the service exchanges small request/response
 * documents and JSONL data rows are re-emitted verbatim from their original
 * bytes, so no general-purpose library is needed.
 */
public final class Json {

    private Json() {
    }

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWhitespace();
        Object v = p.readValue();
        p.skipWhitespace();
        if (p.i < text.length()) {
            throw new IllegalArgumentException("Trailing characters at position " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("Expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    public static byte[] stringifyBytes(Object value) {
        return stringify(value).getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    // ---------------------------------------------------------------- parser

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) {
            this.s = s;
        }

        Object readValue() {
            skipWhitespace();
            if (i >= s.length()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            char c = s.charAt(i);
            switch (c) {
                case '{':
                    return readObject();
                case '[':
                    return readArray();
                case '"':
                    return readString();
                case 't':
                case 'f':
                    return readBoolean();
                case 'n':
                    return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return readNumber();
                    }
                    throw new IllegalArgumentException("Unexpected character '" + c + "' at position " + i);
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWhitespace();
            if (peek() == '}') {
                i++;
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
                    throw new IllegalArgumentException("Expected ',' or '}' at position " + (i - 1));
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWhitespace();
            if (peek() == ']') {
                i++;
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
                    throw new IllegalArgumentException("Expected ',' or ']' at position " + (i - 1));
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (i >= s.length()) {
                    throw new IllegalArgumentException("Unterminated string");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c < 0x20) {
                    throw new IllegalArgumentException("Unescaped control character in string");
                }
                if (c == '\\') {
                    char e = next();
                    switch (e) {
                        case '"':
                            sb.append('"');
                            break;
                        case '\\':
                            sb.append('\\');
                            break;
                        case '/':
                            sb.append('/');
                            break;
                        case 'b':
                            sb.append('\b');
                            break;
                        case 'f':
                            sb.append('\f');
                            break;
                        case 'n':
                            sb.append('\n');
                            break;
                        case 'r':
                            sb.append('\r');
                            break;
                        case 't':
                            sb.append('\t');
                            break;
                        case 'u':
                            if (i + 4 > s.length()) {
                                throw new IllegalArgumentException("Bad \\u escape");
                            }
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                            break;
                        default:
                            throw new IllegalArgumentException("Bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            throw new IllegalArgumentException("Invalid literal at position " + i);
        }

        Object readNull() {
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            throw new IllegalArgumentException("Invalid literal at position " + i);
        }

        Object readNumber() {
            int start = i;
            if (peek() == '-') {
                i++;
            }
            readDigits();
            boolean floating = false;
            if (peek() == '.') {
                floating = true;
                i++;
                readDigits();
            }
            if (peek() == 'e' || peek() == 'E') {
                floating = true;
                i++;
                if (peek() == '+' || peek() == '-') {
                    i++;
                }
                readDigits();
            }
            String token = s.substring(start, i);
            if (!floating) {
                try {
                    return Long.parseLong(token);
                } catch (NumberFormatException e) {
                    return Double.parseDouble(token);
                }
            }
            return Double.parseDouble(token);
        }

        private void readDigits() {
            int start = i;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c >= '0' && c <= '9') {
                    i++;
                } else {
                    break;
                }
            }
            if (i == start) {
                throw new IllegalArgumentException("Expected digit at position " + i);
            }
        }

        void skipWhitespace() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    i++;
                } else {
                    return;
                }
            }
        }

        private char peek() {
            if (i >= s.length()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            return s.charAt(i);
        }

        private char next() {
            if (i >= s.length()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            return s.charAt(i++);
        }

        private void expect(char c) {
            if (i >= s.length() || s.charAt(i) != c) {
                throw new IllegalArgumentException("Expected '" + c + "' at position " + i);
            }
            i++;
        }
    }

    // ------------------------------------------------------------ serializer

    private static void write(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Long || value instanceof Integer) {
            sb.append(value);
        } else if (value instanceof Number) {
            double d = ((Number) value).doubleValue();
            if (d == Math.rint(d) && !Double.isInfinite(d)) {
                sb.append((long) d);
            } else {
                sb.append(d);
            }
        } else if (value instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) value).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) value) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("Cannot serialize type " + value.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
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
