package cdcrebuild.codec;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 极简 JSON 解析 / 序列化器，仅依赖 JDK。
 * 支持对象、数组、字符串、数字（整数取 Long，浮点取 Double）、布尔与 null。
 * 不引入任何第三方依赖，便于在离线环境中用 javac 直接构建。
 */
public final class Json {

    private Json() {
    }

    // ---------------------------------------------------------------- 解析

    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object value = p.readValue();
        p.skipWhitespace();
        if (!p.eof()) {
            throw new IllegalArgumentException("JSON 解析失败: 位置 " + p.pos + " 之后存在多余字符");
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("JSON 解析失败: 顶层不是对象");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        void skipWhitespace() {
            while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
                pos++;
            }
        }

        private char peek() {
            if (eof()) {
                throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 处意外结束");
            }
            return s.charAt(pos);
        }

        private void expect(char c) {
            if (eof() || s.charAt(pos) != c) {
                throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 处应为 '" + c + "'");
            }
            pos++;
        }

        Object readValue() {
            skipWhitespace();
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
                    throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 处出现非法字符 '" + c + "'");
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
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
                char c = peek();
                if (c == '}') {
                    pos++;
                    return map;
                }
                expect(',');
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWhitespace();
            if (peek() == ']') {
                pos++;
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWhitespace();
                char c = peek();
                if (c == ']') {
                    pos++;
                    return list;
                }
                expect(',');
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new IllegalArgumentException("JSON 解析失败: 字符串未闭合");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new IllegalArgumentException("JSON 解析失败: 转义未结束");
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
                        case 'u':
                            if (pos + 4 > s.length()) {
                                throw new IllegalArgumentException("JSON 解析失败: \\u 转义不完整");
                            }
                            String hex = s.substring(pos, pos + 4);
                            sb.append((char) Integer.parseInt(hex, 16));
                            pos += 4;
                            break;
                        default:
                            throw new IllegalArgumentException("JSON 解析失败: 非法转义 \\" + e);
                    }
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
            throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 处非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 处非法字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            while (!eof() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
            boolean floating = false;
            if (!eof() && s.charAt(pos) == '.') {
                floating = true;
                pos++;
                while (!eof() && Character.isDigit(s.charAt(pos))) {
                    pos++;
                }
            }
            if (!eof() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                floating = true;
                pos++;
                if (!eof() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                    pos++;
                }
                while (!eof() && Character.isDigit(s.charAt(pos))) {
                    pos++;
                }
            }
            String token = s.substring(start, pos);
            if (token.isEmpty() || "-".equals(token)) {
                throw new IllegalArgumentException("JSON 解析失败: 位置 " + start + " 处非法数字");
            }
            if (floating) {
                return Double.valueOf(token);
            }
            // 整数一律 Long，避免 WAL 往返后 1 变成 1.0 导致主键规范化不一致
            try {
                return Long.valueOf(token);
            } catch (NumberFormatException nfe) {
                return Double.valueOf(token);
            }
        }
    }

    // ---------------------------------------------------------------- 序列化

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void write(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value.toString());
        } else if (value instanceof Number) {
            sb.append(value.toString());
        } else if (value instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) value).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<Object>) value) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("无法序列化类型: " + value.getClass());
        }
    }

    @SuppressWarnings("unchecked")
    private static void writePretty(StringBuilder sb, Object value, int indent) {
        if (!(value instanceof Map) && !(value instanceof List)) {
            write(sb, value);
            return;
        }
        String pad = "  ".repeat(indent);
        String childPad = "  ".repeat(indent + 1);
        if (value instanceof Map) {
            Map<String, Object> map = (Map<String, Object>) value;
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                sb.append(childPad);
                writeString(sb, e.getKey());
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n').append(pad).append('}');
        } else {
            List<Object> list = (List<Object>) value;
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
                sb.append(childPad);
                writePretty(sb, item, indent + 1);
            }
            sb.append('\n').append(pad).append(']');
        }
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

    // ---------------------------------------------------------------- 规范化形式

    /**
     * 规范化 JSON：对象键按字典序排序、无空白。用于按内容判重
     * （同一位置的重复事件必须与已持久化事件内容一致）。
     */
    public static String canonical(Object value) {
        StringBuilder sb = new StringBuilder();
        canonical(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void canonical(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String || value instanceof Number || value instanceof Boolean) {
            write(sb, value);
        } else if (value instanceof Map) {
            TreeMap<String, Object> sorted = new TreeMap<>(((Map<String, Object>) value));
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : sorted.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                canonical(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<Object>) value) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                canonical(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("无法规范化类型: " + value.getClass());
        }
    }
}
