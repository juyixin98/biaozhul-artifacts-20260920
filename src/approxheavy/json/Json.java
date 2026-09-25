package approxheavy.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal recursive-descent JSON parser/writer — no external dependencies.
 *
 * <p>Maps JSON types onto Java: object {@code -> LinkedHashMap<String,Object>},
 * array {@code -> ArrayList<Object>}, string {@code -> String}, number
 * {@code -> Long} when integral else {@code Double}, true/false/null.
 */
public final class Json {
    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
        this.pos = 0;
    }

    public static Object parse(String text) {
        Json parser = new Json(text);
        parser.skipWhitespace();
        Object value = parser.readValue();
        parser.skipWhitespace();
        if (parser.pos != parser.src.length()) {
            throw new JsonException("trailing characters at position " + parser.pos);
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object value = parse(text);
        if (!(value instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) value;
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    // ---- parsing ----

    private Object readValue() {
        skipWhitespace();
        if (pos >= src.length()) {
            throw new JsonException("unexpected end of input");
        }
        char c = src.charAt(pos);
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
                throw new JsonException("unexpected character '" + c + "' at position " + pos);
        }
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWhitespace();
        if (peek() == '}') {
            pos++;
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
                throw new JsonException("expected ',' or '}' at position " + (pos - 1));
            }
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        expect('[');
        skipWhitespace();
        if (peek() == ']') {
            pos++;
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
                throw new JsonException("expected ',' or ']' at position " + (pos - 1));
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            char c = next();
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char esc = next();
                switch (esc) {
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
                        String hex = src.substring(pos, pos + 4);
                        pos += 4;
                        sb.append((char) Integer.parseInt(hex, 16));
                        break;
                    default:
                        throw new JsonException("bad escape \\" + esc);
                }
            } else {
                sb.append(c);
            }
        }
    }

    private Boolean readBoolean() {
        if (src.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (src.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("invalid literal at position " + pos);
    }

    private Object readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonException("invalid literal at position " + pos);
    }

    private Number readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                pos++;
            } else {
                break;
            }
        }
        String token = src.substring(start, pos);
        if (token.contains(".") || token.contains("e") || token.contains("E")) {
            return Double.parseDouble(token);
        }
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException e) {
            return Double.parseDouble(token);
        }
    }

    private void skipWhitespace() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= src.length()) {
            throw new JsonException("unexpected end of input");
        }
        return src.charAt(pos);
    }

    private char next() {
        if (pos >= src.length()) {
            throw new JsonException("unexpected end of input");
        }
        return src.charAt(pos++);
    }

    private void expect(char c) {
        char actual = next();
        if (actual != c) {
            throw new JsonException("expected '" + c + "' but found '" + actual + "' at " + (pos - 1));
        }
    }

    // ---- writing ----

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value.toString());
        } else if (value instanceof Integer || value instanceof Long) {
            sb.append(value.toString());
        } else if (value instanceof Number) {
            double d = ((Number) value).doubleValue();
            if (d == Math.rint(d) && !Double.isInfinite(d)) {
                sb.append((long) d);
            } else {
                sb.append(value.toString());
            }
        } else if (value instanceof Map) {
            writeObject(sb, (Map<?, ?>) value);
        } else if (value instanceof List) {
            writeArray(sb, (List<?>) value);
        } else {
            throw new JsonException("cannot serialize " + value.getClass());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map) {
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
    }

    private static void writeArray(StringBuilder sb, List<?> list) {
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

    private static void writePretty(StringBuilder sb, Object value, int indent) {
        if (value instanceof Map) {
            Map<?, ?> map = (Map<?, ?>) value;
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (value instanceof List) {
            List<?> list = (List<?>) value;
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writePretty(sb, item, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeValue(sb, value);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        for (int i = 0; i < level; i++) {
            sb.append("  ");
        }
    }

    // ---- typed accessors ----

    public static String str(Map<?, ?> map, String key) {
        Object v = map.get(key);
        if (!(v instanceof String)) {
            throw new JsonException("field '" + key + "' must be a string");
        }
        return (String) v;
    }

    public static String optStr(Map<?, ?> map, String key, String fallback) {
        Object v = map.get(key);
        return v instanceof String ? (String) v : fallback;
    }

    public static long lng(Map<?, ?> map, String key) {
        Object v = map.get(key);
        if (!(v instanceof Number)) {
            throw new JsonException("field '" + key + "' must be a number");
        }
        return ((Number) v).longValue();
    }

    public static long optLng(Map<?, ?> map, String key, long fallback) {
        Object v = map.get(key);
        return v instanceof Number ? ((Number) v).longValue() : fallback;
    }

    public static int integer(Map<?, ?> map, String key) {
        return Math.toIntExact(lng(map, key));
    }

    public static int optInt(Map<?, ?> map, String key, int fallback) {
        Object v = map.get(key);
        return v instanceof Number ? ((Number) v).intValue() : fallback;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> list(Object v) {
        if (!(v instanceof List)) {
            throw new JsonException("expected an array");
        }
        return (List<Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> object(Object v) {
        if (!(v instanceof Map)) {
            throw new JsonException("expected an object");
        }
        return (Map<String, Object>) v;
    }
}
