package phraseindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal, dependency-free JSON reader and writer.
 *
 * Parses into {@code Map<String,Object>}, {@code List<Object>},
 * {@code String}, {@code Number} (Long or Double), {@code Boolean} and
 * {@code null}. The body of JSON this server accepts is small, so recursion
 * depth is bounded by input size.
 */
public final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String input) {
        Json p = new Json(input);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.i != p.s.length()) {
            throw new IllegalArgumentException("trailing data after JSON value at position " + p.i);
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (i >= s.length()) {
            throw new IllegalArgumentException("unexpected end of JSON input");
        }
        char c = s.charAt(i);
        switch (c) {
            case '{': return readObject();
            case '[': return readArray();
            case '"': return readString();
            case 't': return readLiteral("true", Boolean.TRUE);
            case 'f': return readLiteral("false", Boolean.FALSE);
            case 'n': return readLiteral("null", null);
            default: return readNumber();
        }
    }

    private Object readLiteral(String literal, Object value) {
        if (!s.startsWith(literal, i)) {
            throw new IllegalArgumentException("invalid literal at position " + i);
        }
        i += literal.length();
        return value;
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        i++; // {
        skipWs();
        if (i < s.length() && s.charAt(i) == '}') {
            i++;
            return map;
        }
        while (true) {
            skipWs();
            if (i >= s.length() || s.charAt(i) != '"') {
                throw new IllegalArgumentException("expected string key at position " + i);
            }
            String key = readString();
            skipWs();
            if (i >= s.length() || s.charAt(i) != ':') {
                throw new IllegalArgumentException("expected ':' at position " + i);
            }
            i++;
            map.put(key, readValue());
            skipWs();
            if (i >= s.length()) {
                throw new IllegalArgumentException("unterminated object");
            }
            char c = s.charAt(i++);
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new IllegalArgumentException("expected ',' or '}' at position " + (i - 1));
            }
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        i++; // [
        skipWs();
        if (i < s.length() && s.charAt(i) == ']') {
            i++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            if (i >= s.length()) {
                throw new IllegalArgumentException("unterminated array");
            }
            char c = s.charAt(i++);
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new IllegalArgumentException("expected ',' or ']' at position " + (i - 1));
            }
        }
    }

    private String readString() {
        i++; // opening quote
        StringBuilder sb = new StringBuilder();
        while (i < s.length()) {
            char c = s.charAt(i++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (i >= s.length()) {
                    break;
                }
                char e = s.charAt(i++);
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
                            throw new IllegalArgumentException("invalid unicode escape at position " + i);
                        }
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                    }
                    default -> throw new IllegalArgumentException("invalid escape \\" + e);
                }
            } else {
                sb.append(c);
            }
        }
        throw new IllegalArgumentException("unterminated string");
    }

    private Number readNumber() {
        int start = i;
        if (i < s.length() && s.charAt(i) == '-') {
            i++;
        }
        readDigits();
        boolean isDouble = false;
        if (i < s.length() && s.charAt(i) == '.') {
            isDouble = true;
            i++;
            readDigits();
        }
        if (i < s.length() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
            isDouble = true;
            i++;
            if (i < s.length() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                i++;
            }
            readDigits();
        }
        String num = s.substring(start, i);
        if (num.isEmpty() || num.equals("-")) {
            throw new IllegalArgumentException("invalid number at position " + start);
        }
        return isDouble ? Double.parseDouble(num) : Long.parseLong(num);
    }

    private void readDigits() {
        int start = i;
        while (i < s.length() && Character.isDigit(s.charAt(i))) {
            i++;
        }
        if (i == start) {
            throw new IllegalArgumentException("expected digit at position " + i);
        }
    }

    private void skipWs() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }

    // ---- writer ----

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String str) {
            writeString(sb, str);
        } else if (v instanceof Boolean || v instanceof Number) {
            sb.append(v);
        } else if (v instanceof Map<?, ?> map) {
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
        } else if (v instanceof Iterable<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot serialize value of type " + v.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String str) {
        sb.append('"');
        for (int i = 0; i < str.length(); i++) {
            char c = str.charAt(i);
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
