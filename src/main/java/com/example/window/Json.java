package com.example.window;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser/writer with no third-party dependencies.
 *
 * <p>Values are represented as:
 * object -&gt; {@code Map<String,Object>} (LinkedHashMap, insertion order),
 * array -&gt; {@code List<Object>}, string -&gt; String, integer -&gt; Long,
 * floating point -&gt; Double, boolean -&gt; Boolean, null -&gt; null.
 */
public final class Json {

    private Json() {
    }

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    /** Parses a JSON document. Throws {@link JsonException} on malformed input. */
    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object value = p.parseValue();
        p.skipWhitespace();
        if (p.pos != p.text.length()) {
            throw new JsonException("trailing characters (at offset " + p.pos + ")");
        }
        return value;
    }

    /** Serializes a value produced by {@link #parse} (or built by hand) back to JSON. */
    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object value) {
        if (!(value instanceof Map)) {
            throw new JsonException("expected JSON object but got " + describe(value));
        }
        return (Map<String, Object>) value;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object value) {
        if (!(value instanceof List)) {
            throw new JsonException("expected JSON array but got " + describe(value));
        }
        return (List<Object>) value;
    }

    public static String requireString(Map<String, Object> object, String field) {
        Object v = object.get(field);
        if (!(v instanceof String)) {
            throw new JsonException("missing or non-string field '" + field + "'");
        }
        return (String) v;
    }

    public static long requireLong(Map<String, Object> object, String field) {
        Object v = object.get(field);
        if (v instanceof Long) {
            return (Long) v;
        }
        if (v instanceof Double) {
            double d = (Double) v;
            if (!Double.isNaN(d) && !Double.isInfinite(d) && d == Math.rint(d)) {
                return (long) d;
            }
        }
        throw new JsonException("missing or non-integer field '" + field + "'");
    }

    private static String describe(Object v) {
        if (v == null) {
            return "null";
        }
        if (v instanceof String) {
            return "string";
        }
        if (v instanceof Map) {
            return "object";
        }
        if (v instanceof List) {
            return "array";
        }
        if (v instanceof Boolean) {
            return "boolean";
        }
        return "number";
    }

    // ---------- writer ----------

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
            return;
        }
        if (v instanceof String) {
            writeString(sb, (String) v);
            return;
        }
        if (v instanceof Boolean || v instanceof Long || v instanceof Integer) {
            sb.append(v);
            return;
        }
        if (v instanceof Double || v instanceof Float) {
            sb.append(v);
            return;
        }
        if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
            return;
        }
        if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) v) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
            return;
        }
        throw new JsonException("cannot serialize value of type " + v.getClass().getName());
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

    // ---------- parser ----------

    private static final class Parser {
        private final String text;
        private int pos;

        Parser(String text) {
            this.text = text == null ? "" : text;
        }

        void skipWhitespace() {
            while (pos < text.length()) {
                char c = text.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object parseValue() {
            skipWhitespace();
            if (pos >= text.length()) {
                throw error("unexpected end of input");
            }
            char c = text.charAt(pos);
            switch (c) {
                case '{':
                    return parseObject();
                case '[':
                    return parseArray();
                case '"':
                    return parseString();
                case 't':
                    expectLiteral("true");
                    return Boolean.TRUE;
                case 'f':
                    expectLiteral("false");
                    return Boolean.FALSE;
                case 'n':
                    expectLiteral("null");
                    return null;
                default:
                    return parseNumber();
            }
        }

        private Map<String, Object> parseObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // consume '{'
            skipWhitespace();
            if (peek('}')) {
                pos++;
                return map;
            }
            while (true) {
                skipWhitespace();
                if (!peek('"')) {
                    throw error("expected string key");
                }
                String key = parseString();
                skipWhitespace();
                if (!peek(':')) {
                    throw error("expected ':'");
                }
                pos++;
                map.put(key, parseValue());
                skipWhitespace();
                if (peek(',')) {
                    pos++;
                    continue;
                }
                if (peek('}')) {
                    pos++;
                    return map;
                }
                throw error("expected ',' or '}'");
            }
        }

        private List<Object> parseArray() {
            List<Object> list = new ArrayList<>();
            pos++; // consume '['
            skipWhitespace();
            if (peek(']')) {
                pos++;
                return list;
            }
            while (true) {
                list.add(parseValue());
                skipWhitespace();
                if (peek(',')) {
                    pos++;
                    continue;
                }
                if (peek(']')) {
                    pos++;
                    return list;
                }
                throw error("expected ',' or ']'");
            }
        }

        private String parseString() {
            StringBuilder sb = new StringBuilder();
            pos++; // consume opening quote
            while (true) {
                if (pos >= text.length()) {
                    throw error("unterminated string");
                }
                char c = text.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (pos >= text.length()) {
                        throw error("unterminated escape");
                    }
                    char e = text.charAt(pos++);
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
                            if (pos + 4 > text.length()) {
                                throw error("bad \\u escape");
                            }
                            try {
                                sb.append((char) Integer.parseInt(text.substring(pos, pos + 4), 16));
                            } catch (NumberFormatException ex) {
                                throw error("bad \\u escape");
                            }
                            pos += 4;
                            break;
                        default:
                            throw error("bad escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        private Object parseNumber() {
            int start = pos;
            if (peek('-')) {
                pos++;
            }
            while (pos < text.length() && Character.isDigit(text.charAt(pos))) {
                pos++;
            }
            boolean floating = false;
            if (peek('.')) {
                floating = true;
                pos++;
                while (pos < text.length() && Character.isDigit(text.charAt(pos))) {
                    pos++;
                }
            }
            if (peek('e') || peek('E')) {
                floating = true;
                pos++;
                if (peek('+') || peek('-')) {
                    pos++;
                }
                while (pos < text.length() && Character.isDigit(text.charAt(pos))) {
                    pos++;
                }
            }
            if (start == pos) {
                throw error("expected a value");
            }
            String num = text.substring(start, pos);
            try {
                return floating ? (Object) Double.valueOf(num) : (Object) Long.valueOf(num);
            } catch (NumberFormatException e) {
                throw error("bad number '" + num + "'");
            }
        }

        private void expectLiteral(String literal) {
            if (!text.startsWith(literal, pos)) {
                throw error("expected '" + literal + "'");
            }
            pos += literal.length();
        }

        private boolean peek(char c) {
            return pos < text.length() && text.charAt(pos) == c;
        }

        private JsonException error(String msg) {
            return new JsonException(msg + " (at offset " + pos + ")");
        }
    }
}
