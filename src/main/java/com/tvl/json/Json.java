package com.tvl.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小递归下降 JSON 解析器（零外部依赖）。
 * 映射规则：object->LinkedHashMap（保序），array->ArrayList，
 * string->String，true/false->Boolean，null->null，
 * 数字：含 . 或 e/E -> Double，否则 -> Long。
 */
public final class Json {

    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw new JsonException("请求体为空（需要 JSON）");
        }
        Json p = new Json(text);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (p.pos != p.src.length()) {
            throw new JsonException("JSON 解析后仍有多余字符（位置 " + p.pos + "）");
        }
        return value;
    }

    private Object readValue() {
        skipWs();
        if (pos >= src.length()) {
            throw new JsonException("意外的输入结束");
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
                if (c == '-' || Character.isDigit(c)) {
                    return readNumber();
                }
                throw new JsonException("非法 JSON 值起始 '" + c + "'（位置 " + pos + "）");
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
                throw new JsonException("对象键必须是字符串（位置 " + pos + "）");
            }
            String key = readString();
            skipWs();
            if (peek() != ':') {
                throw new JsonException("对象键后缺少 :（位置 " + pos + "）");
            }
            pos++;
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = nextChar();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("对象成员后应为 , 或 }（位置 " + pos + "）");
            }
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
            char c = nextChar();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("数组元素后应为 , 或 ]（位置 " + pos + "）");
            }
        }
    }

    private String readString() {
        pos++; // 开引号
        StringBuilder sb = new StringBuilder();
        while (pos < src.length()) {
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= src.length()) {
                    break;
                }
                char e = src.charAt(pos++);
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
                        if (pos + 4 > src.length()) {
                            throw new JsonException("非法 \\u 转义");
                        }
                        sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("非法字符串转义 \\" + e);
                }
            } else {
                if (c < 0x20) {
                    throw new JsonException("字符串中不允许未转义控制字符（位置 " + pos + "）");
                }
                sb.append(c);
            }
        }
        throw new JsonException("字符串缺少闭合引号");
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
        throw new JsonException("非法字面量（位置 " + pos + "）");
    }

    private Object readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonException("非法字面量（位置 " + pos + "）");
    }

    private Number readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        // JSON 规范：整数部分要么是 0，要么以非零数字开头（禁止 01 这类前导零）
        if (pos >= src.length() || !Character.isDigit(src.charAt(pos))) {
            throw new JsonException("数字格式错误（位置 " + pos + "）");
        }
        if (src.charAt(pos) == '0') {
            pos++;
            if (pos < src.length() && Character.isDigit(src.charAt(pos))) {
                throw new JsonException("数字不允许前导零（位置 " + start + "）");
            }
        } else {
            readDigits();
        }
        boolean isDouble = false;
        if (pos < src.length() && src.charAt(pos) == '.') {
            isDouble = true;
            pos++;
            readDigits();
        }
        if (pos < src.length() && (src.charAt(pos) == 'e' || src.charAt(pos) == 'E')) {
            isDouble = true;
            pos++;
            if (pos < src.length() && (src.charAt(pos) == '+' || src.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        String text = src.substring(start, pos);
        if (isDouble) {
            double d = Double.parseDouble(text);
            if (!Double.isFinite(d)) {
                throw new JsonException("数字超出有限范围: " + text);
            }
            return d;
        }
        try {
            return Long.parseLong(text);
        } catch (NumberFormatException e) {
            throw new JsonException("整数超出 long 范围: " + text);
        }
    }

    private void readDigits() {
        int start = pos;
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            pos++;
        }
        if (pos == start) {
            throw new JsonException("数字格式错误（位置 " + pos + "）");
        }
    }

    private void skipWs() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= src.length()) {
            throw new JsonException("意外的输入结束");
        }
        return src.charAt(pos);
    }

    private char nextChar() {
        if (pos >= src.length()) {
            throw new JsonException("意外的输入结束");
        }
        return src.charAt(pos++);
    }
}
