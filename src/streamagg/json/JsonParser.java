package streamagg.json;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 手写递归下降 JSON 解析器（零依赖）。数字一律解析为 {@link BigDecimal} 以保证精确。
 * 支持标准 JSON：对象、数组、字符串（含全部标准转义与 \\uXXXX）、数字、true/false/null、
 * 对象内重复键以后者为准、空白容忍。
 */
public final class JsonParser {

    private final String input;
    private int pos;

    private JsonParser(String input) {
        this.input = input;
    }

    public static Object parse(String text) {
        JsonParser p = new JsonParser(text);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (p.pos != p.input.length()) {
            throw new JsonException("JSON 解析后存在多余字符，位置 " + p.pos);
        }
        return value;
    }

    private Object readValue() {
        skipWs();
        if (pos >= input.length()) {
            throw new JsonException("意外的输入结尾");
        }
        char c = input.charAt(pos);
        return switch (c) {
            case '{' -> readObject();
            case '[' -> readArray();
            case '"' -> readString();
            case 't', 'f' -> readBoolean();
            case 'n' -> readNull();
            default -> readNumber();
        };
    }

    private Map<String, Object> readObject() {
        expect('{');
        Map<String, Object> map = new LinkedHashMap<>();
        skipWs();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            String key = readString();
            skipWs();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("对象中期望 ',' 或 '}'，位置 " + (pos - 1));
            }
        }
    }

    private List<Object> readArray() {
        expect('[');
        List<Object> list = new ArrayList<>();
        skipWs();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("数组中期望 ',' 或 ']'，位置 " + (pos - 1));
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= input.length()) {
                throw new JsonException("字符串未闭合");
            }
            char c = input.charAt(pos++);
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
                        if (pos + 4 > input.length()) {
                            throw new JsonException("\\uXXXX 转义不完整");
                        }
                        String hex = input.substring(pos, pos + 4);
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException nfe) {
                            throw new JsonException("非法 \\u 转义: " + hex);
                        }
                        pos += 4;
                    }
                    default -> throw new JsonException("非法转义: \\" + e);
                }
            } else {
                if (c < 0x20) {
                    throw new JsonException("字符串中存在未转义控制字符");
                }
                sb.append(c);
            }
        }
    }

    private Boolean readBoolean() {
        if (input.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (input.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private Object readNull() {
        if (input.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private BigDecimal readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        readIntDigits();
        if (pos < input.length() && input.charAt(pos) == '.') {
            pos++;
            readIntDigits();
        }
        if (pos < input.length() && (input.charAt(pos) == 'e' || input.charAt(pos) == 'E')) {
            pos++;
            if (pos < input.length() && (input.charAt(pos) == '+' || input.charAt(pos) == '-')) {
                pos++;
            }
            readIntDigits();
        }
        String token = input.substring(start, pos);
        if (token.isEmpty() || "-".equals(token)) {
            throw new JsonException("非法数字，位置 " + start);
        }
        try {
            return new BigDecimal(token);
        } catch (NumberFormatException nfe) {
            throw new JsonException("非法数字: " + token);
        }
    }

    private void readIntDigits() {
        int start = pos;
        while (pos < input.length() && Character.isDigit(input.charAt(pos))) {
            pos++;
        }
        if (pos == start) {
            throw new JsonException("数字缺少整数部分，位置 " + pos);
        }
    }

    private void skipWs() {
        while (pos < input.length() && Character.isWhitespace(input.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= input.length()) {
            throw new JsonException("意外的输入结尾");
        }
        return input.charAt(pos);
    }

    private char next() {
        if (pos >= input.length()) {
            throw new JsonException("意外的输入结尾");
        }
        return input.charAt(pos++);
    }

    private void expect(char c) {
        char actual = next();
        if (actual != c) {
            throw new JsonException("期望 '" + c + "'，实际 '" + actual + "'，位置 " + (pos - 1));
        }
    }
}
