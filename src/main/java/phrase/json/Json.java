package phrase.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小 JSON 实现（仅依赖 JDK）：
 * <ul>
 *   <li>{@link #parse(String)} 解析对象/数组/字符串/数字/true/false/null，
 *       JSON 对象用 {@link LinkedHashMap} 保序，JSON 数组用 {@link ArrayList}，
 *       整数解析为 Long，小数/指数解析为 Double；</li>
 *   <li>{@link #stringify(Object)} 紧凑序列化，{@link #pretty(Object)} 两空格缩进。</li>
 * </ul>
 * 不是完整高性能 JSON 库，但覆盖本服务全部 I/O，避免引入外部依赖。
 */
public final class Json {

    private Json() {}

    // ---------- 序列化 ----------

    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value, 0, false);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value, 0, true);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object v, int depth, boolean pretty) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean || v instanceof Number) {
            sb.append(v.toString());
        } else if (v instanceof CharSequence || v instanceof Character) {
            writeString(sb, v.toString());
        } else if (v instanceof Map<?, ?> map) {
            writeMap(sb, map, depth, pretty);
        } else if (v instanceof Iterable<?> it) {
            writeArray(sb, it, depth, pretty);
        } else if (v.getClass().isArray()) {
            writeArray(sb, arrayAsList(v), depth, pretty);
        } else {
            writeString(sb, v.toString());
        }
    }

    private static List<Object> arrayAsList(Object array) {
        List<Object> out = new ArrayList<>();
        int len = java.lang.reflect.Array.getLength(array);
        for (int i = 0; i < len; i++) {
            out.add(java.lang.reflect.Array.get(array, i));
        }
        return out;
    }

    private static void writeMap(StringBuilder sb, Map<?, ?> map, int depth, boolean pretty) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            if (pretty) {
                sb.append('\n');
                indent(sb, depth + 1);
            }
            writeString(sb, e.getKey().toString());
            sb.append(pretty ? ": " : ":");
            write(sb, e.getValue(), depth + 1, pretty);
        }
        if (pretty) {
            sb.append('\n');
            indent(sb, depth);
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, Iterable<?> items, int depth, boolean pretty) {
        boolean first = true;
        if (pretty) {
            List<Object> list = new ArrayList<>();
            items.forEach(list::add);
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('\n');
                indent(sb, depth + 1);
                write(sb, item, depth + 1, true);
            }
            sb.append('\n');
            indent(sb, depth);
            sb.append(']');
        } else {
            sb.append('[');
            for (Object item : items) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(sb, item, depth + 1, false);
            }
            sb.append(']');
        }
    }

    private static void indent(StringBuilder sb, int depth) {
        sb.append("  ".repeat(depth));
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

    // ---------- 解析 ----------

    public static Object parse(String input) {
        Parser p = new Parser(input);
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
        private int pos;

        Parser(String s) {
            this.s = s == null ? "" : s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        IllegalArgumentException error(String msg) {
            return new IllegalArgumentException("JSON parse error at position " + pos + ": " + msg);
        }

        void skipWs() {
            while (!eof() && Character.isWhitespace(s.charAt(pos))) {
                pos++;
            }
        }

        Object readValue() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            char c = s.charAt(pos);
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> {
                    if (c == '-' || c == '+') {
                        yield readNumber();
                    }
                    if (c >= '0' && c <= '9') {
                        yield readNumber();
                    }
                    throw error("unexpected character '" + c + "'");
                }
            };
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWs();
                if (peek() != '"') {
                    throw error("expected string key");
                }
                String key = readString();
                skipWs();
                expect(':');
                skipWs();
                Object value = readValue();
                map.put(key, value);
                skipWs();
                char c = next();
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
                pos++;
                return list;
            }
            while (true) {
                skipWs();
                list.add(readValue());
                skipWs();
                char c = next();
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
                if (eof()) {
                    throw error("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw error("unterminated escape");
                    }
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'u' -> {
                            if (pos + 4 > s.length()) {
                                throw error("bad unicode escape");
                            }
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw error("bad escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Object readBoolean() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw error("invalid literal");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw error("invalid literal");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-' || peek() == '+') {
                pos++;
            }
            boolean isDouble = false;
            while (!eof()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    isDouble = true;
                    pos++;
                } else {
                    break;
                }
            }
            String token = s.substring(start, pos);
            if (isDouble) {
                return Double.parseDouble(token);
            }
            return Long.parseLong(token);
        }

        char peek() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            return s.charAt(pos);
        }

        char next() {
            if (eof()) {
                throw error("unexpected end of input");
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw error("expected '" + c + "' but got '" + actual + "'");
            }
        }
    }
}
