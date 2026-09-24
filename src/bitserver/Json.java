package bitserver;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简手写 JSON 解析器（零外部依赖）。
 *
 * 类型映射：object -> LinkedHashMap，array -> ArrayList，string -> String，
 * 整数 -> Long，小数/指数 -> Double，true/false/null -> Boolean/null。
 * 对输入做严格校验，任何非法输入抛出 {@link JsonException}（HTTP 层转 400）。
 */
public final class Json {

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private final String s;
    private int pos;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        Json p = new Json(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.s.length()) {
            throw new JsonException("JSON 解析失败：末尾存在多余字符（位置 " + p.pos + "）");
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：意外的输入结尾");
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
                throw new JsonException("JSON 解析失败：意外字符 '" + c + "'（位置 " + pos + "）");
        }
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        pos++; // {
        skipWs();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            if (peek() != '"') {
                throw new JsonException("JSON 解析失败：对象键必须是字符串（位置 " + pos + "）");
            }
            String key = readString();
            skipWs();
            if (next() != ':') {
                throw new JsonException("JSON 解析失败：对象中缺少 ':'（位置 " + pos + "）");
            }
            Object val = readValue();
            map.put(key, val);
            skipWs();
            char c = next();
            if (c == ',') {
                continue;
            }
            if (c == '}') {
                return map;
            }
            throw new JsonException("JSON 解析失败：对象中应为 ',' 或 '}'（位置 " + pos + "）");
        }
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        pos++; // [
        skipWs();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            char c = next();
            if (c == ',') {
                continue;
            }
            if (c == ']') {
                return list;
            }
            throw new JsonException("JSON 解析失败：数组中应为 ',' 或 ']'（位置 " + pos + "）");
        }
    }

    private String readString() {
        pos++; // 开引号
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) {
                throw new JsonException("JSON 解析失败：字符串未闭合");
            }
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw new JsonException("JSON 解析失败：转义未结束");
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
                            throw new JsonException("JSON 解析失败：\\u 转义不完整");
                        }
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("JSON 解析失败：非法转义 \\" + e);
                }
            } else if (c < 0x20) {
                throw new JsonException("JSON 解析失败：字符串内存在未转义控制字符");
            } else {
                sb.append(c);
            }
        }
    }

    private Object readLiteral(String literal, Object value) {
        if (!s.startsWith(literal, pos)) {
            throw new JsonException("JSON 解析失败：非法字面量（位置 " + pos + "）");
        }
        pos += literal.length();
        return value;
    }

    private Number readNumber() {
        int start = pos;
        if (pos < s.length() && s.charAt(pos) == '-') {
            pos++;
        }
        readDigits();
        boolean isDouble = false;
        if (pos < s.length() && s.charAt(pos) == '.') {
            isDouble = true;
            pos++;
            readDigits();
        }
        if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
            isDouble = true;
            pos++;
            if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        String num = s.substring(start, pos);
        try {
            // 注意：不能用三元表达式，Double/Long 混合会被统一提升为 double
            if (isDouble) {
                return Double.valueOf(Double.parseDouble(num));
            }
            return Long.valueOf(Long.parseLong(num));
        } catch (NumberFormatException e) {
            throw new JsonException("JSON 解析失败：非法数字 " + num);
        }
    }

    private void readDigits() {
        int start = pos;
        while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
            pos++;
        }
        if (pos == start) {
            throw new JsonException("JSON 解析失败：缺少数字（位置 " + pos + "）");
        }
    }

    private void skipWs() {
        while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：意外的输入结尾");
        }
        return s.charAt(pos);
    }

    private char next() {
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：意外的输入结尾");
        }
        return s.charAt(pos++);
    }

    // ---------------------- 序列化 ----------------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Number) {
            if (value instanceof Double d && (d.isNaN() || d.isInfinite())) {
                throw new IllegalArgumentException("JSON 不支持 NaN/Infinity");
            }
            sb.append(value);
        } else if (value instanceof Map<?, ?>) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) value).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List<?>) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<Object>) value) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("不支持的 JSON 类型: " + value.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
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
