package orderedevents.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser (RFC 8259 subset used by the HTTP API):
 * objects preserve insertion order, numbers parse as Long when integral and
 * Double otherwise. Intentionally small — request bodies are hand-authored
 * specs, not arbitrary documents.
 */
public final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw new JsonException("empty body");
        }
        Json p = new Json(text);
        p.skipWs();
        Object v = p.value();
        p.skipWs();
        if (p.i != p.s.length()) {
            throw new JsonException("trailing characters at position " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private Object value() {
        skipWs();
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        char c = s.charAt(i);
        return switch (c) {
            case '{' -> object();
            case '[' -> array();
            case '"' -> string();
            case 't', 'f' -> bool();
            case 'n' -> nul();
            default -> number();
        };
    }

    private Map<String, Object> object() {
        Map<String, Object> m = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') {
            i++;
            return m;
        }
        while (true) {
            skipWs();
            String key = string();
            skipWs();
            expect(':');
            m.put(key, value());
            skipWs();
            char c = next();
            if (c == '}') {
                return m;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at position " + (i - 1));
            }
        }
    }

    private List<Object> array() {
        List<Object> a = new ArrayList<>();
        expect('[');
        skipWs();
        if (peek() == ']') {
            i++;
            return a;
        }
        while (true) {
            a.add(value());
            skipWs();
            char c = next();
            if (c == ']') {
                return a;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or ']' at position " + (i - 1));
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
                            throw new JsonException("bad unicode escape");
                        }
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                    }
                    default -> throw new JsonException("bad escape \\" + e);
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
        throw new JsonException("invalid literal at position " + i);
    }

    private Object nul() {
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        throw new JsonException("invalid literal at position " + i);
    }

    private Object number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        while (i < s.length() && Character.isDigit(s.charAt(i))) {
            i++;
        }
        boolean floating = false;
        if (i < s.length() && s.charAt(i) == '.') {
            floating = true;
            i++;
            while (i < s.length() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
        if (i < s.length() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
            floating = true;
            i++;
            if (i < s.length() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                i++;
            }
            while (i < s.length() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
        String tok = s.substring(start, i);
        if (tok.isEmpty() || "-".equals(tok)) {
            throw new JsonException("invalid number at position " + start);
        }
        if (floating) {
            return Double.parseDouble(tok);
        }
        try {
            return Long.parseLong(tok);
        } catch (NumberFormatException e) {
            return Double.parseDouble(tok);
        }
    }

    private void skipWs() {
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
            throw new JsonException("expected '" + c + "' at position " + i);
        }
        i++;
    }
}
