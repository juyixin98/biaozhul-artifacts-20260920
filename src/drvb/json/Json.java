package drvb.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极小的零依赖 JSON 解析 / 序列化工具。
 *
 * <p>Java 类型映射：
 * <ul>
 *   <li>object -&gt; {@link LinkedHashMap}</li>
 *   <li>array  -&gt; {@link ArrayList}</li>
 *   <li>string -&gt; {@link String}</li>
 *   <li>number -&gt; {@link Long} 或 {@link Double}（整数量被解析为 Long）</li>
 *   <li>true/false -&gt; {@link Boolean}，null -&gt; {@link #NULL}</li>
 * </ul>
 */
public final class Json {

    private Json() {
    }

    /** JSON null 的单例哨兵（区分 "键不存在" 与 "值为 null"）。 */
    public static final Object NULL = new Object() {
        @Override
        public String toString() {
            return "null";
        }
    };

    // ------------------------------------------------------------------
    // 构造辅助
    // ------------------------------------------------------------------

    /** 以交替 key/value 参数构造一个保持插入顺序的 JSON 对象。 */
    public static Map<String, Object> obj(Object... kv) {
        if ((kv.length & 1) != 0) {
            throw new IllegalArgumentException("obj() 需要成对的 key/value 参数");
        }
        LinkedHashMap<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }

    public static List<Object> arr(Object... values) {
        ArrayList<Object> list = new ArrayList<>();
        for (Object v : values) {
            list.add(v);
        }
        return list;
    }

    // ------------------------------------------------------------------
    // 解析
    // ------------------------------------------------------------------

    public static Object parse(String input) {
        Parser p = new Parser(input);
        p.skipWhitespace();
        Object value = p.readValue();
        p.skipWhitespace();
        if (p.pos < p.input.length()) {
            throw p.error("JSON 解析后存在多余内容");
        }
        return value;
    }

    private static final class Parser {
        private final String input;
        private int pos;

        Parser(String input) {
            this.input = input;
        }

        JsonException error(String msg) {
            return new JsonException(msg + "（位置 " + pos + "）");
        }

        void skipWhitespace() {
            while (pos < input.length()) {
                char c = input.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object readValue() {
            if (pos >= input.length()) {
                throw error("意外的输入结束");
            }
            char c = input.charAt(pos);
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
                    throw error("无法识别的 JSON 值，起始字符: " + c);
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            LinkedHashMap<String, Object> map = new LinkedHashMap<>();
            skipWhitespace();
            if (peek() == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWhitespace();
                if (peek() != '"') {
                    throw error("对象键必须是字符串");
                }
                String key = readString();
                skipWhitespace();
                expect(':');
                skipWhitespace();
                Object value = readValue();
                map.put(key, value);
                skipWhitespace();
                char c = next();
                if (c == '}') {
                    return map;
                }
                if (c != ',') {
                    throw error("对象成员之间需要 ','，实际: " + c);
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            ArrayList<Object> list = new ArrayList<>();
            skipWhitespace();
            if (peek() == ']') {
                pos++;
                return list;
            }
            while (true) {
                skipWhitespace();
                list.add(readValue());
                skipWhitespace();
                char c = next();
                if (c == ']') {
                    return list;
                }
                if (c != ',') {
                    throw error("数组元素之间需要 ','，实际: " + c);
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= input.length()) {
                    throw error("字符串未闭合");
                }
                char c = input.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (pos >= input.length()) {
                        throw error("转义序列未完成");
                    }
                    char e = input.charAt(pos++);
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
                            if (pos + 4 > input.length()) {
                                throw error("\\u 转义需要 4 位十六进制数");
                            }
                            String hex = input.substring(pos, pos + 4);
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException nfe) {
                                throw error("非法的 \\u 转义: " + hex);
                            }
                            pos += 4;
                            break;
                        default:
                            throw error("非法的转义字符: \\" + e);
                    }
                } else if (c < 0x20) {
                    throw error("字符串中不允许出现未转义的控制字符: " + (int) c);
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (input.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (input.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw error("非法的字面量");
        }

        Object readNull() {
            if (input.startsWith("null", pos)) {
                pos += 4;
                return NULL;
            }
            throw error("非法的字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            readIntegerDigits();
            boolean isDouble = false;
            if (pos < input.length() && input.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                readIntegerDigits();
            }
            if (pos < input.length() && (input.charAt(pos) == 'e' || input.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (pos < input.length() && (input.charAt(pos) == '+' || input.charAt(pos) == '-')) {
                    pos++;
                }
                readIntegerDigits();
            }
            String token = input.substring(start, pos);
            try {
                if (!isDouble) {
                    return Long.valueOf(Long.parseLong(token));
                }
                return Double.valueOf(Double.parseDouble(token));
            } catch (NumberFormatException nfe) {
                throw error("非法数字: " + token);
            }
        }

        void readIntegerDigits() {
            int start = pos;
            while (pos < input.length() && Character.isDigit(input.charAt(pos))) {
                pos++;
            }
            if (pos == start) {
                throw error("数字中缺少数位");
            }
        }

        char peek() {
            if (pos >= input.length()) {
                throw error("意外的输入结束");
            }
            return input.charAt(pos);
        }

        char next() {
            if (pos >= input.length()) {
                throw error("意外的输入结束");
            }
            return input.charAt(pos++);
        }

        void expect(char c) {
            if (pos >= input.length() || input.charAt(pos) != c) {
                throw error("期望字符 '" + c + "'");
            }
            pos++;
        }
    }

    // ------------------------------------------------------------------
    // 序列化
    // ------------------------------------------------------------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeCompact(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeCompact(StringBuilder sb, Object value) {
        if (value == null || value == NULL) {
            sb.append("null");
        } else if (value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Number) {
            Number n = (Number) value;
            if (n instanceof Double) {
                double d = (Double) n;
                if (Double.isNaN(d) || Double.isInfinite(d)) {
                    throw new JsonException("JSON 不允许 NaN/Infinity");
                }
                sb.append(d);
            } else if (n instanceof Float) {
                double d = ((Float) n).doubleValue();
                if (Float.isNaN((Float) n) || Float.isInfinite((Float) n)) {
                    throw new JsonException("JSON 不允许 NaN/Infinity");
                }
                sb.append(d);
            } else {
                sb.append(n);
            }
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) value).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeCompact(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object item : (Iterable<?>) value) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeCompact(sb, item);
            }
            sb.append(']');
        } else if (value instanceof Object[]) {
            writeCompact(sb, java.util.Arrays.asList((Object[]) value));
        } else {
            throw new JsonException("无法序列化为 JSON 的类型: " + value.getClass().getName());
        }
    }

    private static void writeIndented(StringBuilder sb, Object value, int depth) {
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
                indent(sb, depth + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writeIndented(sb, e.getValue(), depth + 1);
            }
            sb.append('\n');
            indent(sb, depth);
            sb.append('}');
        } else if (value instanceof Iterable) {
            java.util.Iterator<?> it = ((Iterable<?>) value).iterator();
            if (!it.hasNext()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            while (it.hasNext()) {
                indent(sb, depth + 1);
                writeIndented(sb, it.next(), depth + 1);
                if (it.hasNext()) {
                    sb.append(',');
                }
                sb.append('\n');
            }
            indent(sb, depth);
            sb.append(']');
        } else {
            writeCompact(sb, value);
        }
    }

    private static void indent(StringBuilder sb, int depth) {
        for (int i = 0; i < depth; i++) {
            sb.append("  ");
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

    // ------------------------------------------------------------------
    // 类型化访问
    // ------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object v) {
        if (!(v instanceof Map)) {
            throw new JsonException("期望 JSON 对象，实际: " + typeName(v));
        }
        return (Map<String, Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object v) {
        if (!(v instanceof List)) {
            throw new JsonException("期望 JSON 数组，实际: " + typeName(v));
        }
        return (List<Object>) v;
    }

    public static String asString(Object v) {
        if (!(v instanceof String)) {
            throw new JsonException("期望字符串，实际: " + typeName(v));
        }
        return (String) v;
    }

    public static boolean asBoolean(Object v) {
        if (!(v instanceof Boolean)) {
            throw new JsonException("期望布尔值，实际: " + typeName(v));
        }
        return (Boolean) v;
    }

    public static double asDouble(Object v) {
        if (!(v instanceof Number)) {
            throw new JsonException("期望数字，实际: " + typeName(v));
        }
        return ((Number) v).doubleValue();
    }

    public static long asLong(Object v) {
        if (!(v instanceof Number)) {
            throw new JsonException("期望整数，实际: " + typeName(v));
        }
        return ((Number) v).longValue();
    }

    public static boolean isNull(Object v) {
        return v == null || v == NULL;
    }

    public static String getString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (isNull(v)) {
            throw new JsonException("字段 '" + key + "' 必须是非空字符串");
        }
        return asString(v);
    }

    public static String getOptionalString(Map<String, Object> m, String key, String dflt) {
        Object v = m.get(key);
        return v == null ? dflt : asString(v);
    }

    public static long getLong(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            throw new JsonException("缺少必填整型字段 '" + key + "'");
        }
        return asLong(v);
    }

    private static String typeName(Object v) {
        if (v == null || v == NULL) {
            return "null";
        }
        if (v instanceof Map) {
            return "object";
        }
        if (v instanceof List) {
            return "array";
        }
        return v.getClass().getSimpleName();
    }
}
