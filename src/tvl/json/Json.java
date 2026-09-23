package tvl.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小但完整的 JSON 解析器/书写器（无外部依赖）。
 *
 * 解析结果使用标准 Java 类型表示：
 *   对象   -> LinkedHashMap<String,Object>（保留键顺序）
 *   数组   -> ArrayList<Object>
 *   字符串 -> String
 *   整数   -> Long
 *   小数   -> Double
 *   布尔   -> Boolean
 *   null   -> null
 */
public final class Json {

    private Json() {
    }

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("trailing characters after JSON value", p.pos);
        }
        return value;
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    // ---------------------------------------------------------------------
    // 书写
    // ---------------------------------------------------------------------

    private static void writeTo(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Long l) {
            sb.append(l.longValue());
        } else if (value instanceof Integer i) {
            sb.append(i.intValue());
        } else if (value instanceof Double d) {
            sb.append(d.doubleValue());
        } else if (value instanceof Number n) {
            sb.append(n.toString());
        } else if (value instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object o : it) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, o);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot serialize value of type " + value.getClass());
        }
    }

    private static void writePretty(StringBuilder sb, Object value, int indent) {
        if (value instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (value instanceof Iterable<?> it && !(value instanceof String)) {
            List<Object> list = new ArrayList<>();
            for (Object o : it) {
                list.add(o);
            }
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object o : list) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                writePretty(sb, o, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, value);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
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

    // ---------------------------------------------------------------------
    // 解析
    // ---------------------------------------------------------------------

    private static final class Parser {
        private final String text;
        private int pos;

        Parser(String text) {
            this.text = text;
        }

        boolean eof() {
            return pos >= text.length();
        }

        char peek() {
            return text.charAt(pos);
        }

        void skipWs() {
            while (!eof()) {
                char c = peek();
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object readValue() {
            if (eof()) throw new JsonException("unexpected end of input, expected a JSON value", pos);
            char c = peek();
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> {
                    if (c == '-' || (c >= '0' && c <= '9')) yield readNumber();
                    throw new JsonException("unexpected character '" + c + "', expected JSON value", pos);
                }
            };
        }

        Map<String, Object> readObject() {
            LinkedHashMap<String, Object> map = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (consumeIf('}')) return map;
            while (true) {
                skipWs();
                if (eof() || peek() != '"') {
                    throw new JsonException("expected a string key in object", pos);
                }
                String key = readString();
                skipWs();
                if (!consumeIf(':')) {
                    throw new JsonException("expected ':' after object key", pos);
                }
                skipWs();
                Object value = readValue();
                map.put(key, value);
                skipWs();
                if (consumeIf(',')) continue;
                if (consumeIf('}')) break;
                throw new JsonException("expected ',' or '}' in object", pos);
            }
            return map;
        }

        List<Object> readArray() {
            ArrayList<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (consumeIf(']')) return list;
            while (true) {
                skipWs();
                list.add(readValue());
                skipWs();
                if (consumeIf(',')) continue;
                if (consumeIf(']')) break;
                throw new JsonException("expected ',' or ']' in array", pos);
            }
            return list;
        }

        String readString() {
            int start = pos;
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new JsonException("unterminated string", start);
                char c = text.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\n' || c == '\r') {
                    throw new JsonException("unterminated string (newline in string)", pos);
                }
                if (c == '\\') {
                    if (eof()) throw new JsonException("unterminated escape sequence", pos);
                    char e = text.charAt(pos++);
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
                            if (pos + 4 > text.length()) {
                                throw new JsonException("incomplete \\u escape", pos);
                            }
                            String hex = text.substring(pos, pos + 4);
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException ex) {
                                throw new JsonException("invalid \\u escape: " + hex, pos);
                            }
                            pos += 4;
                        }
                        default -> throw new JsonException("invalid escape '\\" + e + "'", pos - 1);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (text.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (text.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new JsonException("invalid literal (did you mean true/false?)", pos);
        }

        Object readNull() {
            if (text.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonException("invalid literal (did you mean null?)", pos);
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            if (eof() || !Character.isDigit(peek())) {
                throw new JsonException("invalid number", start);
            }
            // JSON 不允许前导零：01 非法，0.1 合法
            if (peek() == '0' && pos + 1 < text.length()
                    && Character.isDigit(text.charAt(pos + 1))) {
                throw new JsonException("leading zeros are not allowed in numbers", pos);
            }
            while (!eof() && Character.isDigit(peek())) pos++;
            boolean isDouble = false;
            if (!eof() && peek() == '.') {
                isDouble = true;
                pos++;
                if (eof() || !Character.isDigit(peek())) {
                    throw new JsonException("invalid number: digits expected after '.'", pos);
                }
                while (!eof() && Character.isDigit(peek())) pos++;
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                isDouble = true;
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) pos++;
                if (eof() || !Character.isDigit(peek())) {
                    throw new JsonException("invalid number: digits expected after exponent", pos);
                }
                while (!eof() && Character.isDigit(peek())) pos++;
            }
            String token = text.substring(start, pos);
            if (isDouble) {
                try {
                    return Double.parseDouble(token);
                } catch (NumberFormatException ex) {
                    throw new JsonException("invalid number '" + token + "'", start);
                }
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException ex) {
                throw new JsonException("integer literal out of range '" + token + "'", start);
            }
        }

        void expect(char c) {
            if (eof() || peek() != c) {
                throw new JsonException("expected '" + c + "'", pos);
            }
            pos++;
        }

        boolean consumeIf(char c) {
            if (!eof() && peek() == c) {
                pos++;
                return true;
            }
            return false;
        }
    }
}
