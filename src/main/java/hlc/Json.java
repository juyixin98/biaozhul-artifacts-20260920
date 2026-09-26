package hlc;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal, dependency-free JSON parser/serializer for the fixed request/response shapes of
 * this backend.
 *
 * <p>Supported values: {@link Map}, {@link List}, {@link String}, {@link Long},
 * {@link Double}, {@link Boolean}, {@code null}. This is intentionally small, not a general
 * purpose library; all input is validated at the parser boundary and again by callers.
 */
public final class Json {

    private Json() {
    }

    // ------------------------------------------------------------------ parsing

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new HLCException("expected a JSON object at the top level");
        }
        return (Map<String, Object>) v;
    }

    public static Object parse(String text) {
        Parser p = new Parser(text == null ? "" : text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw p.error("trailing characters after JSON value");
        }
        return v;
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

        HLCException error(String msg) {
            return new HLCException("invalid JSON at position " + i + ": " + msg);
        }

        void skipWs() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        char peek() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            return s.charAt(i);
        }

        Object readValue() {
            skipWs();
            char c = peek();
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
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                i++;
                return map;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object value = readValue();
                map.put(key, value);
                skipWs();
                char c = nextChar();
                if (c == '}') {
                    return map;
                }
                if (c != ',') {
                    throw error("expected ',' or '}'");
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (peek() == ']') {
                i++;
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = nextChar();
                if (c == ']') {
                    return list;
                }
                if (c != ',') {
                    throw error("expected ',' or ']'");
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = nextChar();
                if (c == '"') {
                    return sb.toString();
                }
                if (c < 0x20) {
                    throw error("unescaped control character in string");
                }
                if (c == '\\') {
                    char e = nextChar();
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
                            int code = 0;
                            for (int k = 0; k < 4; k++) {
                                char h = nextChar();
                                int d = Character.digit(h, 16);
                                if (d < 0) {
                                    throw error("bad unicode escape");
                                }
                                code = code * 16 + d;
                            }
                            sb.append((char) code);
                        }
                        default -> throw error("bad escape \\" + e);
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
            throw error("invalid literal");
        }

        Object readNull() {
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            throw error("invalid literal");
        }

        Object readNumber() {
            int start = i;
            if (peek() == '-') {
                i++;
            }
            readDigits();
            boolean isDouble = false;
            if (!eof() && s.charAt(i) == '.') {
                isDouble = true;
                i++;
                readDigits();
            }
            if (!eof() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                isDouble = true;
                i++;
                if (!eof() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                    i++;
                }
                readDigits();
            }
            String token = s.substring(start, i);
            if (token.isEmpty() || "-".equals(token)) {
                throw error("invalid number");
            }
            if (isDouble) {
                return Double.parseDouble(token);
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException e) {
                throw error("number out of range: " + token);
            }
        }

        private void readDigits() {
            int start = i;
            while (i < s.length() && Character.digit(s.charAt(i), 10) >= 0) {
                i++;
            }
            if (i == start) {
                throw error("expected digits");
            }
        }

        private char nextChar() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            return s.charAt(i++);
        }

        private void expect(char c) {
            if (eof() || s.charAt(i) != c) {
                throw error("expected '" + c + "'");
            }
            i++;
        }
    }

    // ------------------------------------------------------------------ writing

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Long l) {
            sb.append(l.longValue());
        } else if (v instanceof Integer n) {
            sb.append(n.intValue());
        } else if (v instanceof Double d) {
            sb.append(d.doubleValue());
        } else if (v instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) map).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object o : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, o);
            }
            sb.append(']');
        } else {
            throw new HLCException("cannot serialize value of type " + v.getClass().getName());
        }
    }

    @SuppressWarnings("unchecked")
    private static void writeIndented(StringBuilder sb, Object v, int depth) {
        if (v instanceof Map<?, ?> map && !map.isEmpty()) {
            sb.append("{\n");
            String indent = "  ".repeat(depth + 1);
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) map).entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                sb.append(indent);
                writeString(sb, e.getKey());
                sb.append(": ");
                writeIndented(sb, e.getValue(), depth + 1);
            }
            sb.append('\n').append("  ".repeat(depth)).append('}');
        } else if (v instanceof List<?> list && !list.isEmpty()) {
            sb.append("[\n");
            String indent = "  ".repeat(depth + 1);
            boolean first = true;
            for (Object o : list) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                sb.append(indent);
                writeIndented(sb, o, depth + 1);
            }
            sb.append('\n').append("  ".repeat(depth)).append(']');
        } else {
            writeValue(sb, v);
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
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

    // ------------------------------------------------------------------ helpers

    public static String requireString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new HLCException("field '" + key + "' must be a non-empty string");
        }
        return s;
    }

    public static long requireLong(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v instanceof Long l) {
            return l;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new HLCException("field '" + key + "' must be an integer");
    }

    public static String optionalString(Map<String, Object> obj, String key, String dflt) {
        Object v = obj.get(key);
        return v == null ? dflt : v.toString();
    }
}
