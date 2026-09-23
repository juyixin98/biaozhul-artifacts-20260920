package dev.dedup.hll;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器（零外部依赖）。
 * 支持 object/array/string/number/true/false/null。
 * 数字规则：无小数点/指数且落在 long 范围内解析为 Long，否则为 Double。
 * 非法输入抛出 {@link IllegalArgumentException}，错误信息带行列号。
 */
public final class Json {

    private final String s;
    private int pos;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        if (text == null) {
            throw new JsonException("输入为 null");
        }
        Json p = new Json(text);
        p.skipWhitespace();
        Object v = p.readValue();
        p.skipWhitespace();
        if (p.pos != p.s.length()) {
            throw p.error("JSON 结束后仍有多余字符");
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("顶层不是 JSON 对象");
        }
        return (Map<String, Object>) v;
    }

    private Object readValue() {
        skipWhitespace();
        if (pos >= s.length()) {
            throw error("意外结束");
        }
        char c = s.charAt(pos);
        switch (c) {
            case '{':
                return readObject();
            case '[':
                return readArray();
            case '"':
                return readString();
            case 't':
                readLiteral("true");
                return Boolean.TRUE;
            case 'f':
                readLiteral("false");
                return Boolean.FALSE;
            case 'n':
                readLiteral("null");
                return null;
            default:
                if (c == '-' || (c >= '0' && c <= '9')) {
                    return readNumber();
                }
                throw error("意外字符 '" + c + "'");
        }
    }

    private void readLiteral(String lit) {
        if (pos + lit.length() > s.length() || !s.startsWith(lit, pos)) {
            throw error("期望字面量 " + lit);
        }
        pos += lit.length();
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
            if (peek() != '"') {
                throw error("对象键必须是字符串");
            }
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
                throw error("对象中期望 ',' 或 '}'，得到 '" + c + "'");
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
                throw error("数组中期望 ',' 或 ']'，得到 '" + c + "'");
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) {
                throw error("字符串未闭合");
            }
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c < 0x20) {
                throw error("字符串内存在未转义控制字符 U+" + Integer.toHexString(c));
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw error("转义未结束");
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
                            throw error("\\u 转义不完整");
                        }
                        String hex = s.substring(pos, pos + 4);
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException nfe) {
                            throw error("非法 \\u 转义: " + hex);
                        }
                        pos += 4;
                        break;
                    default:
                        throw error("非法转义 '\\" + e + "'");
                }
            } else {
                sb.append(c);
            }
        }
    }

    private Number readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        requireDigit();
        // 整数部分：JSON 允许 -0 / 0、12；不允许多余前导零（01），严格按规范处理
        if (peek() == '0') {
            pos++;
        } else {
            readDigits();
        }
        boolean isDouble = false;
        if (peek() == '.') {
            isDouble = true;
            pos++;
            requireDigit();
            readDigits();
        }
        char c = peekSafe();
        if (c == 'e' || c == 'E') {
            isDouble = true;
            pos++;
            c = peekSafe();
            if (c == '+' || c == '-') {
                pos++;
            }
            requireDigit();
            readDigits();
        }
        String num = s.substring(start, pos);
        if (isDouble) {
            double d = Double.parseDouble(num);
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw error("JSON 数字不能为 NaN/Infinity");
            }
            return d;
        }
        try {
            return Long.parseLong(num);
        } catch (NumberFormatException nfe) {
            // 超出 long 范围的整数按 double 兜底
            return Double.parseDouble(num);
        }
    }

    private void requireDigit() {
        if (pos >= s.length() || !Character.isDigit(s.charAt(pos))) {
            throw error("数字格式错误");
        }
    }

    private void readDigits() {
        while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
            pos++;
        }
    }

    /** peek 的非抛出版：数字内部越界交给后续 requireDigit 报错。 */
    private char peekSafe() {
        return pos < s.length() ? s.charAt(pos) : '\0';
    }

    private void skipWhitespace() {
        while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= s.length()) {
            throw error("意外结束");
        }
        return s.charAt(pos);
    }

    private char next() {
        if (pos >= s.length()) {
            throw error("意外结束");
        }
        return s.charAt(pos++);
    }

    private void expect(char c) {
        if (pos >= s.length() || s.charAt(pos) != c) {
            throw error("期望 '" + c + "'");
        }
        pos++;
    }

    private JsonException error(String msg) {
        int line = 1;
        int col = 1;
        for (int i = 0; i < pos && i < s.length(); i++) {
            if (s.charAt(i) == '\n') {
                line++;
                col = 1;
            } else {
                col++;
            }
        }
        return new JsonException(msg + "（第 " + line + " 行第 " + col + " 列）");
    }

    /** JSON 序列化：支持 Map/List/String/Number/Boolean/null，pretty=true 时两空格缩进。 */
    public static String write(Object value, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value, pretty, 0);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v, boolean pretty, int depth) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Integer || v instanceof Long) {
            sb.append(v);
        } else if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("JSON 不能序列化 NaN/Infinity");
            }
            sb.append(v);
        } else if (v instanceof Map) {
            writeObject(sb, (Map<?, ?>) v, pretty, depth);
        } else if (v instanceof List) {
            writeArray(sb, (List<?>) v, pretty, depth);
        } else {
            throw new IllegalArgumentException("不可 JSON 序列化的类型: " + v.getClass());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map, boolean pretty, int depth) {
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
            newline(sb, pretty, depth + 1);
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(pretty ? ": " : ":");
            writeValue(sb, e.getValue(), pretty, depth + 1);
        }
        newline(sb, pretty, depth);
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<?> list, boolean pretty, int depth) {
        if (list.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline(sb, pretty, depth + 1);
            writeValue(sb, item, pretty, depth + 1);
        }
        newline(sb, pretty, depth);
        sb.append(']');
    }

    private static void newline(StringBuilder sb, boolean pretty, int depth) {
        if (!pretty) {
            return;
        }
        sb.append('\n');
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

    /** JSON 语法错误。 */
    public static class JsonException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        JsonException(String message) {
            super(message);
        }
    }
}
