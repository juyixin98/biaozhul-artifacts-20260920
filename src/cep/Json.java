package cep;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 工具，仅使用 JDK。
 *
 * 解析结果的 Java 映射：
 *   object -> LinkedHashMap&lt;String, Object&gt;（保留字段顺序）
 *   array  -> ArrayList&lt;Object&gt;
 *   string -> String
 *   number -> Long（能装进 long 时）或 Double
 *   true/false -> Boolean
 *   null -> null
 *
 * 输入要求是合法 JSON；非法输入抛出 JsonException（RuntimeException），
 * 由 HTTP 层转成 400 响应，绝不静默吞掉。
 */
public final class Json {

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private Json() {
    }

    // ---------------------------------------------------------------- 解析

    public static Object parse(String input) {
        Parser p = new Parser(input);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("JSON 解析失败: 位置 " + p.pos + " 之后存在多余字符");
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String input) {
        Object v = parse(input);
        if (!(v instanceof Map)) {
            throw new JsonException("JSON 解析失败: 顶层不是对象");
        }
        return (Map<String, Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> parseArray(String input) {
        Object v = parse(input);
        if (!(v instanceof List)) {
            throw new JsonException("JSON 解析失败: 顶层不是数组");
        }
        return (List<Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        char peek() {
            return s.charAt(pos);
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

        void expect(char c) {
            if (eof() || s.charAt(pos) != c) {
                throw new JsonException("JSON 解析失败: 位置 " + pos + " 期望 '" + c + "'");
            }
            pos++;
        }

        Object readValue() {
            if (eof()) {
                throw new JsonException("JSON 解析失败: 位置 " + pos + " 意外结束");
            }
            char c = peek();
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
                    throw new JsonException("JSON 解析失败: 位置 " + pos + " 非法字符 '" + c + "'");
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWs();
            if (!eof() && peek() == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                skipWs();
                Object value = readValue();
                map.put(key, value);
                skipWs();
                if (eof()) {
                    throw new JsonException("JSON 解析失败: 对象未闭合");
                }
                char c = s.charAt(pos++);
                if (c == '}') {
                    return map;
                }
                if (c != ',') {
                    throw new JsonException("JSON 解析失败: 位置 " + (pos - 1) + " 期望 ',' 或 '}'");
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (!eof() && peek() == ']') {
                pos++;
                return list;
            }
            while (true) {
                skipWs();
                list.add(readValue());
                skipWs();
                if (eof()) {
                    throw new JsonException("JSON 解析失败: 数组未闭合");
                }
                char c = s.charAt(pos++);
                if (c == ']') {
                    return list;
                }
                if (c != ',') {
                    throw new JsonException("JSON 解析失败: 位置 " + (pos - 1) + " 期望 ',' 或 ']'");
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new JsonException("JSON 解析失败: 字符串未闭合");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new JsonException("JSON 解析失败: 转义序列未完成");
                    }
                    char e = s.charAt(pos++);
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
                            if (pos + 4 > s.length()) {
                                throw new JsonException("JSON 解析失败: \\u 转义长度不足");
                            }
                            String hex = s.substring(pos, pos + 4);
                            pos += 4;
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException ex) {
                                throw new JsonException("JSON 解析失败: 非法 \\u 转义 " + hex);
                            }
                            break;
                        }
                        default:
                            throw new JsonException("JSON 解析失败: 非法转义 \\" + e);
                    }
                } else if (c < 0x20) {
                    throw new JsonException("JSON 解析失败: 字符串内含未转义控制字符 (0x"
                            + Integer.toHexString(c) + ")");
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new JsonException("JSON 解析失败: 位置 " + pos + " 非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonException("JSON 解析失败: 位置 " + pos + " 非法字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            while (!eof()) {
                char c = peek();
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    pos++;
                } else {
                    break;
                }
            }
            String token = s.substring(start, pos);
            try {
                if (token.indexOf('.') < 0 && token.indexOf('e') < 0 && token.indexOf('E') < 0) {
                    return Long.parseLong(token);
                }
                return Double.parseDouble(token);
            } catch (NumberFormatException ex) {
                throw new JsonException("JSON 解析失败: 非法数字 " + token);
            }
        }
    }

    // ------------------------------------------------------------- 序列化

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Integer || value instanceof Long) {
            sb.append(value);
        } else if (value instanceof Double || value instanceof Float) {
            double d = ((Number) value).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new JsonException("JSON 序列化失败: 数值为 NaN/Infinity");
            }
            sb.append(value);
        } else if (value instanceof Map) {
            writeObject(sb, (Map<?, ?>) value);
        } else if (value instanceof Iterable) {
            writeArray(sb, (Iterable<?>) value);
        } else {
            throw new JsonException("JSON 序列化失败: 不支持的类型 " + value.getClass().getName());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(sb, String.valueOf(entry.getKey()));
            sb.append(':');
            writeValue(sb, entry.getValue());
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, Iterable<?> items) {
        sb.append('[');
        boolean first = true;
        for (Object item : items) {
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
}
