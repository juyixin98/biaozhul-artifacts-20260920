package com.example.quantiles.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 手写的递归下降 JSON 解析器（零依赖，仅支持本服务所需的标准 JSON 子集）。
 * 输出的对象类型为 {@link Json} 的嵌套记录。
 */
public final class JsonParser {

    private final String src;
    private int pos;

    private JsonParser(String src) {
        this.src = src;
    }

    public static Object parse(String text) {
        JsonParser p = new JsonParser(text);
        p.skipWhitespace();
        Object v = p.readValue();
        p.skipWhitespace();
        if (p.pos != p.src.length()) {
            throw new JsonException("JSON 解析后存在多余字符，位置 " + p.pos);
        }
        return v;
    }

    private Object readValue() {
        skipWhitespace();
        if (pos >= src.length()) {
            throw new JsonException("意外的输入结尾");
        }
        char c = src.charAt(pos);
        return switch (c) {
            case '{' -> readObject();
            case '[' -> readArray();
            case '"' -> new Json.JString(readString());
            case 't', 'f' -> readBool();
            case 'n' -> readNull();
            default -> {
                if (c == '-' || (c >= '0' && c <= '9')) {
                    yield readNumber();
                }
                throw new JsonException("意外字符 '" + c + "'，位置 " + pos);
            }
        };
    }

    private Json.JObject readObject() {
        expect('{');
        Map<String, Object> members = new LinkedHashMap<>();
        skipWhitespace();
        if (peek() == '}') {
            pos++;
            return new Json.JObject(members);
        }
        while (true) {
            skipWhitespace();
            String key = readString();
            skipWhitespace();
            expect(':');
            Object value = readValue();
            members.put(key, value);
            skipWhitespace();
            char c = next();
            if (c == '}') {
                break;
            }
            if (c != ',') {
                throw new JsonException("对象中期望 ',' 或 '}'，位置 " + (pos - 1));
            }
        }
        return new Json.JObject(members);
    }

    private Json.JArray readArray() {
        expect('[');
        List<Object> elements = new ArrayList<>();
        skipWhitespace();
        if (peek() == ']') {
            pos++;
            return new Json.JArray(elements);
        }
        while (true) {
            elements.add(readValue());
            skipWhitespace();
            char c = next();
            if (c == ']') {
                break;
            }
            if (c != ',') {
                throw new JsonException("数组中期望 ',' 或 ']'，位置 " + (pos - 1));
            }
        }
        return new Json.JArray(elements);
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new JsonException("字符串未闭合");
            }
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char e = next();
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
                        if (pos + 4 > src.length()) {
                            throw new JsonException("\\u 转义不完整");
                        }
                        String hex = src.substring(pos, pos + 4);
                        pos += 4;
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException nfe) {
                            throw new JsonException("非法 \\u 转义: " + hex);
                        }
                    }
                    default -> throw new JsonException("非法转义字符 \\" + e);
                }
            } else if (c < 0x20) {
                throw new JsonException("字符串中存在未转义控制字符，位置 " + (pos - 1));
            } else {
                sb.append(c);
            }
        }
    }

    private Json.JNumber readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        readDigits();
        boolean isFloating = false;
        if (peek() == '.') {
            isFloating = true;
            pos++;
            readDigits();
        }
        if (peek() == 'e' || peek() == 'E') {
            isFloating = true;
            pos++;
            if (peek() == '+' || peek() == '-') {
                pos++;
            }
            readDigits();
        }
        String raw = src.substring(start, pos);
        try {
            if (isFloating) {
                Double.parseDouble(raw);
            } else {
                Long.parseLong(raw);
            }
        } catch (NumberFormatException nfe) {
            throw new JsonException("非法数字: " + raw);
        }
        return new Json.JNumber(raw);
    }

    private void readDigits() {
        int start = pos;
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            pos++;
        }
        if (pos == start) {
            throw new JsonException("期望数字，位置 " + pos);
        }
    }

    private Json.JBool readBool() {
        if (src.startsWith("true", pos)) {
            pos += 4;
            return new Json.JBool(true);
        }
        if (src.startsWith("false", pos)) {
            pos += 5;
            return new Json.JBool(false);
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private Json.JNull readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return new Json.JNull();
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private void skipWhitespace() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        return pos < src.length() ? src.charAt(pos) : '\0';
    }

    private char next() {
        if (pos >= src.length()) {
            throw new JsonException("意外的输入结尾");
        }
        return src.charAt(pos++);
    }

    private void expect(char c) {
        char actual = next();
        if (actual != c) {
            throw new JsonException("期望 '" + c + "' 实际 '" + actual + "'，位置 " + (pos - 1));
        }
    }
}
