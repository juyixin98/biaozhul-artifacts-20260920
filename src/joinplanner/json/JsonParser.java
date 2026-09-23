package joinplanner.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal recursive-descent JSON parser.
 * Supported values: Map<String,Object>, List<Object>, String, Double, Boolean, null.
 * All JSON numbers are parsed as double; callers convert as needed.
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
            throw new JsonException("Trailing characters at position " + p.pos);
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (pos >= s.length()) {
            throw new JsonException("Unexpected end of input");
        }
        char c = s.charAt(pos);
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
                throw new JsonException("Unexpected character '" + c + "' at position " + pos);
        }
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        pos++; // {
        skipWs();
        if (pos < s.length() && s.charAt(pos) == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            if (pos >= s.length() || s.charAt(pos) != '"') {
                throw new JsonException("Expected string key at position " + pos);
            }
            String key = readString();
            skipWs();
            if (pos >= s.length() || s.charAt(pos) != ':') {
                throw new JsonException("Expected ':' at position " + pos);
            }
            pos++;
            Object value = readValue();
            map.put(key, value);
            skipWs();
            if (pos >= s.length()) {
                throw new JsonException("Unterminated object");
            }
            char c = s.charAt(pos);
            pos++;
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("Expected ',' or '}' at position " + (pos - 1));
            }
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        pos++; // [
        skipWs();
        if (pos < s.length() && s.charAt(pos) == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            if (pos >= s.length()) {
                throw new JsonException("Unterminated array");
            }
            char c = s.charAt(pos);
            pos++;
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("Expected ',' or ']' at position " + (pos - 1));
            }
        }
    }

    private String readString() {
        if (s.charAt(pos) != '"') {
            throw new JsonException("Expected '\"' at position " + pos);
        }
        pos++;
        StringBuilder sb = new StringBuilder();
        while (pos < s.length()) {
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw new JsonException("Unterminated escape");
                }
                char e = s.charAt(pos++);
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
                        if (pos + 4 > s.length()) {
                            throw new JsonException("Bad unicode escape");
                        }
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("Invalid escape \\" + e);
                }
            } else {
                sb.append(c);
            }
        }
        throw new JsonException("Unterminated string");
    }

    private Object readLiteral(String literal, Object value) {
        if (!s.startsWith(literal, pos)) {
            throw new JsonException("Invalid literal at position " + pos);
        }
        pos += literal.length();
        return value;
    }

    private Double readNumber() {
        int start = pos;
        if (s.charAt(pos) == '-') {
            pos++;
        }
        readDigits();
        if (pos < s.length() && s.charAt(pos) == '.') {
            pos++;
            readDigits();
        }
        if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
            pos++;
            if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        return Double.parseDouble(s.substring(start, pos));
    }

    private void readDigits() {
        int start = pos;
        while (pos < s.length() && s.charAt(pos) >= '0' && s.charAt(pos) <= '9') {
            pos++;
        }
        if (start == pos) {
            throw new JsonException("Expected digit at position " + pos);
        }
    }

    private void skipWs() {
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }
}
