package vecsearch.json;

import vecsearch.util.ApiException;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小 JSON 解析器（RFC 8259 子集），零第三方依赖。
 *
 * <p>映射到 Java 类型：object -> LinkedHashMap（保序），array -> ArrayList，
 * string -> String，true/false -> Boolean，null -> null，数字 -> Double。
 * 输入非法时抛出 400 ApiException。
 */
public final class Json {

    private final String s;
    private int pos;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw ApiException.badRequest("request body is empty (expected JSON)");
        }
        Json p = new Json(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.s.length()) {
            throw ApiException.badRequest("trailing characters after JSON value at pos " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw ApiException.badRequest("expected a JSON object at top level");
        }
        return (Map<String, Object>) v;
    }

    private Object readValue() {
        skipWs();
        if (pos >= s.length()) {
            throw ApiException.badRequest("unexpected end of JSON");
        }
        char c = s.charAt(pos);
        return switch (c) {
            case '{' -> readObject();
            case '[' -> readArray();
            case '"' -> readString();
            case 't', 'f' -> readBool();
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
            String key = readString();
            skipWs();
            expect(':');
            Object val = readValue();
            map.put(key, val);
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw ApiException.badRequest("expected ',' or '}' at pos " + (pos - 1));
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
                throw ApiException.badRequest("expected ',' or ']' at pos " + (pos - 1));
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) {
                throw ApiException.badRequest("unterminated string");
            }
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw ApiException.badRequest("unterminated escape");
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
                            throw ApiException.badRequest("bad \\u escape");
                        }
                        String hex = s.substring(pos, pos + 4);
                        pos += 4;
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException nfe) {
                            throw ApiException.badRequest("bad \\u escape: " + hex);
                        }
                    }
                    default -> throw ApiException.badRequest("bad escape \\" + e);
                }
            } else {
                if (c < 0x20) {
                    throw ApiException.badRequest("unescaped control char in string");
                }
                sb.append(c);
            }
        }
    }

    private Boolean readBool() {
        if (s.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw ApiException.badRequest("invalid literal at pos " + pos);
    }

    private Object readNull() {
        if (s.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw ApiException.badRequest("invalid literal at pos " + pos);
    }

    private Double readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                pos++;
            } else {
                break;
            }
        }
        String tok = s.substring(start, pos);
        if (tok.isEmpty()) {
            throw ApiException.badRequest("invalid value at pos " + start);
        }
        try {
            return Double.parseDouble(tok);
        } catch (NumberFormatException nfe) {
            throw ApiException.badRequest("invalid number: " + tok);
        }
    }

    private void skipWs() {
        while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        return pos < s.length() ? s.charAt(pos) : '\0';
    }

    private char next() {
        if (pos >= s.length()) {
            throw ApiException.badRequest("unexpected end of JSON");
        }
        return s.charAt(pos++);
    }

    private void expect(char c) {
        if (pos >= s.length() || s.charAt(pos) != c) {
            throw ApiException.badRequest("expected '" + c + "' at pos " + pos);
        }
        pos++;
    }
}
