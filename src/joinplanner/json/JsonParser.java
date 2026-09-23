package joinplanner.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal recursive-descent JSON parser (RFC 8259 subset, no external dependencies).
 *
 * <p>Values are represented as:
 * {@link LinkedHashMap}&lt;String,Object&gt;, {@link ArrayList}&lt;Object&gt;,
 * {@link String}, {@link Double}, {@link Long}, {@link Boolean}, {@code null}.
 * Numbers without fraction/exponent parse as Long when they fit.
 */
public final class JsonParser {

    private final String s;
    private int pos;

    private JsonParser(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        JsonParser p = new JsonParser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.s.length()) {
            throw new JsonParseException("Trailing characters at position " + p.pos);
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (pos >= s.length()) {
            throw new JsonParseException("Unexpected end of input");
        }
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

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            if (peek() != '"') {
                throw new JsonParseException("Expected string key at position " + pos);
            }
            String key = readString();
            skipWs();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonParseException("Expected ',' or '}' at position " + (pos - 1));
            }
        }
    }

    private List<Object> readArray() {
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
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonParseException("Expected ',' or ']' at position " + (pos - 1));
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) {
                throw new JsonParseException("Unterminated string");
            }
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw new JsonParseException("Unterminated escape");
                }
                char e = s.charAt(pos++);
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
                        if (pos + 4 > s.length()) {
                            throw new JsonParseException("Bad unicode escape");
                        }
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                    }
                    default -> throw new JsonParseException("Invalid escape \\" + e);
                }
            } else if (c < 0x20) {
                throw new JsonParseException("Unescaped control character in string");
            } else {
                sb.append(c);
            }
        }
    }

    private Boolean readBoolean() {
        if (s.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw new JsonParseException("Invalid literal at position " + pos);
    }

    private Object readNull() {
        if (s.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonParseException("Invalid literal at position " + pos);
    }

    private Number readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        readDigits();
        boolean isDouble = false;
        if (pos < s.length() && s.charAt(pos) == '.') {
            isDouble = true;
            pos++;
            readDigits();
        }
        if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
            isDouble = true;
            pos++;
            if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        String token = s.substring(start, pos);
        if (token.isEmpty() || token.equals("-")) {
            throw new JsonParseException("Invalid number at position " + start);
        }
        if (isDouble) {
            return Double.parseDouble(token);
        }
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException ex) {
            return Double.parseDouble(token);
        }
    }

    private void readDigits() {
        if (pos >= s.length() || !Character.isDigit(s.charAt(pos))) {
            throw new JsonParseException("Expected digit at position " + pos);
        }
        while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
            pos++;
        }
    }

    private void skipWs() {
        while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= s.length()) {
            throw new JsonParseException("Unexpected end of input");
        }
        return s.charAt(pos);
    }

    private char next() {
        if (pos >= s.length()) {
            throw new JsonParseException("Unexpected end of input");
        }
        return s.charAt(pos++);
    }

    private void expect(char c) {
        if (pos >= s.length() || s.charAt(pos) != c) {
            throw new JsonParseException("Expected '" + c + "' at position " + pos);
        }
        pos++;
    }
}
