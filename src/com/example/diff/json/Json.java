package com.example.diff.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser/serializer.
 *
 * <p>Supports objects (insertion-ordered LinkedHashMap), arrays (List),
 * strings, numbers (parsed as Long or Double), booleans and null. Enough for
 * the service's request/response contracts; no external library needed.
 */
public final class Json {

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String input) {
        if (input == null || input.isBlank()) {
            throw new JsonException("empty JSON input");
        }
        Json p = new Json(input);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.i != p.s.length()) {
            throw new JsonException("trailing characters at position " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String input) {
        Object v = parse(input);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private Object value() {
        ws();
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        char c = s.charAt(i);
        switch (c) {
            case '{':
                return object();
            case '[':
                return array();
            case '"':
                return string();
            case 't':
            case 'f':
                return bool();
            case 'n':
                return nul();
            default:
                return number();
        }
    }

    private Map<String, Object> object() {
        Map<String, Object> m = new LinkedHashMap<>();
        expect('{');
        ws();
        if (peek() == '}') {
            i++;
            return m;
        }
        while (true) {
            ws();
            if (peek() != '"') {
                throw new JsonException("expected string key at " + i);
            }
            String key = string();
            ws();
            expect(':');
            Object val = value();
            m.put(key, val);
            ws();
            char c = next();
            if (c == '}') {
                return m;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at " + (i - 1));
            }
        }
    }

    private List<Object> array() {
        List<Object> list = new java.util.ArrayList<>();
        expect('[');
        ws();
        if (peek() == ']') {
            i++;
            return list;
        }
        while (true) {
            list.add(value());
            ws();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or ']' at " + (i - 1));
            }
        }
    }

    private String string() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (i >= s.length()) {
                throw new JsonException("unterminated string");
            }
            char c = s.charAt(i++);
            if (c == '"') {
                return sb.toString();
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
                    case 'u': {
                        if (i + 4 > s.length()) {
                            throw new JsonException("bad unicode escape");
                        }
                        int cp = Integer.parseInt(s.substring(i, i + 4), 16);
                        sb.append((char) cp);
                        i += 4;
                        break;
                    }
                    default:
                        throw new JsonException("bad escape \\" + e);
                }
            } else {
                sb.append(c);
            }
        }
    }

    private Boolean bool() {
        if (s.startsWith("true", i)) {
            i += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", i)) {
            i += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("invalid literal at " + i);
    }

    private Object nul() {
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        throw new JsonException("invalid literal at " + i);
    }

    private Number number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        while (i < s.length() && Character.isDigit(s.charAt(i))) {
            i++;
        }
        boolean isDouble = false;
        if (i < s.length() && s.charAt(i) == '.') {
            isDouble = true;
            i++;
            while (i < s.length() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
        if (i < s.length() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
            isDouble = true;
            i++;
            if (i < s.length() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                i++;
            }
            while (i < s.length() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
        String tok = s.substring(start, i);
        if (tok.isEmpty() || tok.equals("-")) {
            throw new JsonException("invalid number at " + start);
        }
        // Box explicitly: a `double : long` ternary would undergo binary
        // numeric promotion and silently turn the long branch into a double.
        if (isDouble) {
            return Double.valueOf(Double.parseDouble(tok));
        }
        return Long.valueOf(Long.parseLong(tok));
    }

    private void ws() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }

    private char peek() {
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        return s.charAt(i);
    }

    private char next() {
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        return s.charAt(i++);
    }

    private void expect(char c) {
        if (i >= s.length() || s.charAt(i) != c) {
            throw new JsonException("expected '" + c + "' at " + i);
        }
        i++;
    }

    // ---------------------------------------------------------------
    // Serialization
    // ---------------------------------------------------------------

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    public static String writePretty(Object v) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, v, 0);
        return sb.toString();
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            int n = 0;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                pad(sb, indent + 1);
                writeStr(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
                if (++n < m.size()) {
                    sb.append(',');
                }
                sb.append('\n');
            }
            pad(sb, indent);
            sb.append('}');
        } else if (v instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            for (int i = 0; i < list.size(); i++) {
                pad(sb, indent + 1);
                writePretty(sb, list.get(i), indent + 1);
                if (i < list.size() - 1) {
                    sb.append(',');
                }
                sb.append('\n');
            }
            pad(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, v);
        }
    }

    private static void pad(StringBuilder sb, int indent) {
        sb.append("  ".repeat(indent));
    }

    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String str) {
            writeStr(sb, str);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Number n) {
            if (n instanceof Double d && (d.isInfinite() || d.isNaN())) {
                throw new JsonException("non-finite double cannot be serialized");
            }
            sb.append(n);
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeStr(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List<?> list) {
            sb.append('[');
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                writeTo(sb, list.get(i));
            }
            sb.append(']');
        } else {
            writeStr(sb, String.valueOf(v));
        }
    }

    private static void writeStr(StringBuilder sb, String str) {
        sb.append('"');
        for (int i = 0; i < str.length(); i++) {
            char c = str.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
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
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
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
