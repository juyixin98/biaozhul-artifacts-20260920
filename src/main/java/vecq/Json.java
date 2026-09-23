package vecq;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简手写 JSON 解析 / 序列化（无任何外部依赖）。
 *
 * 值模型：
 *   null    -> Java null
 *   true/false -> Boolean
 *   整数    -> Long
 *   小数    -> Double
 *   字符串  -> String
 *   数组    -> List&lt;Object&gt;
 *   对象    -> LinkedHashMap&lt;String,Object&gt;（保留键顺序）
 */
public final class Json {

    private Json() {}

    public static final class JsonException extends RuntimeException {
        public JsonException(String msg) { super(msg); }
        public JsonException(String msg, Throwable cause) { super(msg, cause); }
    }

    // ---------------- 解析 ----------------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos < p.text.length()) {
            throw p.error("尾部存在多余字符");
        }
        return v;
    }

    private static final class Parser {
        final String text;
        int pos;

        Parser(String text) { this.text = text; }

        JsonException error(String msg) {
            return new JsonException(msg + "（位置 " + pos + "）");
        }

        void skipWs() {
            while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) pos++;
        }

        char peek() {
            if (pos >= text.length()) throw error("意外的输入结束");
            return text.charAt(pos);
        }

        Object readValue() {
            skipWs();
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': case 'f': return readBoolean();
                case 'n': return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw error("无法识别的 JSON 值，首字符 '" + c + "'");
            }
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (peek() == '}') { pos++; return map; }
            while (true) {
                skipWs();
                if (peek() != '"') throw error("对象键必须是字符串");
                String key = readString();
                skipWs();
                expect(':');
                Object val = readValue();
                map.put(key, val);
                skipWs();
                char c = next();
                if (c == ',') continue;
                if (c == '}') break;
                throw error("对象成员之间需要 ',' 或 '}'，实际 '" + c + "'");
            }
            return map;
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (peek() == ']') { pos++; return list; }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ',') continue;
                if (c == ']') break;
                throw error("数组元素之间需要 ',' 或 ']'，实际 '" + c + "'");
            }
            return list;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= text.length()) throw error("字符串未闭合");
                char c = text.charAt(pos++);
                if (c == '"') break;
                if (c == '\\') {
                    if (pos >= text.length()) throw error("转义序列不完整");
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
                            if (pos + 4 > text.length()) throw error("\\uXXXX 不完整");
                            try {
                                sb.append((char) Integer.parseInt(text.substring(pos, pos + 4), 16));
                            } catch (NumberFormatException ex) {
                                throw error("非法的 \\uXXXX 转义");
                            }
                            pos += 4;
                        }
                        default -> throw error("非法转义字符 \\" + e);
                    }
                } else if (c < 0x20) {
                    throw error("字符串中不允许出现未转义控制字符");
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Boolean readBoolean() {
            if (text.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (text.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw error("非法字面量");
        }

        Object readNull() {
            if (text.startsWith("null", pos)) { pos += 4; return null; }
            throw error("非法字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            readDigits();
            boolean isDouble = false;
            if (pos < text.length() && text.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                readDigits();
            }
            if (pos < text.length() && (text.charAt(pos) == 'e' || text.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (pos < text.length() && (text.charAt(pos) == '+' || text.charAt(pos) == '-')) pos++;
                readDigits();
            }
            String token = text.substring(start, pos);
            try {
                if (!isDouble) return Long.parseLong(token);
                return Double.parseDouble(token);
            } catch (NumberFormatException ex) {
                throw error("非法数字 " + token);
            }
        }

        void readDigits() {
            int start = pos;
            while (pos < text.length() && Character.isDigit(text.charAt(pos))) pos++;
            if (pos == start) throw error("需要数字");
        }

        void expect(char c) {
            if (pos >= text.length() || text.charAt(pos) != c) {
                throw error("期望 '" + c + "'");
            }
            pos++;
        }

        char next() {
            if (pos >= text.length()) throw error("意外的输入结束");
            return text.charAt(pos++);
        }
    }

    // ---------------- 序列化 ----------------

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    public static String writePretty(Object v) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, v, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> map) {
            if (map.isEmpty()) { sb.append("{}"); return; }
            sb.append("{\n");
            int i = 0;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
                if (++i < map.size()) sb.append(',');
                sb.append('\n');
            }
            indent(sb, indent);
            sb.append('}');
        } else if (v instanceof List<?> list) {
            if (list.isEmpty()) { sb.append("[]"); return; }
            sb.append("[\n");
            for (int i = 0; i < list.size(); i++) {
                indent(sb, indent + 1);
                writePretty(sb, list.get(i), indent + 1);
                if (i < list.size() - 1) sb.append(',');
                sb.append('\n');
            }
            indent(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, v);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean || v instanceof Number) {
            sb.append(v);
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List<?> list) {
            sb.append('[');
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(',');
                writeTo(sb, list.get(i));
            }
            sb.append(']');
        } else if (v instanceof int[] arr) {
            sb.append('[');
            for (int i = 0; i < arr.length; i++) {
                if (i > 0) sb.append(',');
                sb.append(arr[i]);
            }
            sb.append(']');
        } else if (v instanceof Object[] arr) {
            sb.append('[');
            for (int i = 0; i < arr.length; i++) {
                if (i > 0) sb.append(',');
                writeTo(sb, arr[i]);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
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
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    // ---------------- 类型辅助 ----------------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asMap(Object o) {
        if (!(o instanceof Map<?, ?>)) throw new JsonException("期望 JSON 对象，实际为 " + typeName(o));
        return (Map<String, Object>) o;
    }

    public static List<Object> asList(Object o) {
        if (!(o instanceof List<?>)) throw new JsonException("期望 JSON 数组，实际为 " + typeName(o));
        @SuppressWarnings("unchecked")
        List<Object> l = (List<Object>) o;
        return l;
    }

    public static String typeName(Object o) {
        if (o == null) return "null";
        if (o instanceof Map) return "object";
        if (o instanceof List) return "array";
        if (o instanceof String) return "string";
        if (o instanceof Boolean) return "boolean";
        return "number";
    }
}
