package cep.json;

/**
 * 手写递归下降 JSON 解析器（零依赖）。支持对象、数组、字符串（含标准转义/uXXXX）、
 * 数字（整数/小数/指数）、true/false/null 与空白。
 */
public final class JsonParser {

    private final String src;
    private int pos;

    private JsonParser(String src) {
        this.src = src;
    }

    public static JsonValue parse(String text) {
        if (text == null || text.isBlank()) {
            throw new JsonException("请求体为空");
        }
        JsonParser p = new JsonParser(text);
        p.skipWs();
        JsonValue v = p.readValue();
        p.skipWs();
        if (p.pos != p.src.length()) {
            throw p.error("JSON 结束后还有多余字符");
        }
        return v;
    }

    private JsonValue readValue() {
        skipWs();
        if (pos >= src.length()) {
            throw error("意外结束");
        }
        char c = src.charAt(pos);
        return switch (c) {
            case '{' -> readObj();
            case '[' -> readArr();
            case '"' -> new JsonValue.Str(readString());
            case 't', 'f' -> readBool();
            case 'n' -> readNull();
            default -> readNumber();
        };
    }

    private JsonValue.Obj readObj() {
        JsonValue.Obj obj = JsonValue.obj();
        expect('{');
        skipWs();
        if (peek() == '}') {
            pos++;
            return obj;
        }
        while (true) {
            skipWs();
            String key = readString();
            skipWs();
            expect(':');
            JsonValue value = readValue();
            obj.set(key, value);
            skipWs();
            char c = next();
            if (c == '}') {
                return obj;
            }
            if (c != ',') {
                throw error("期望 ',' 或 '}'，遇到 " + c);
            }
        }
    }

    private JsonValue.Arr readArr() {
        JsonValue.Arr arr = JsonValue.arr();
        expect('[');
        skipWs();
        if (peek() == ']') {
            pos++;
            return arr;
        }
        while (true) {
            arr.add(readValue());
            skipWs();
            char c = next();
            if (c == ']') {
                return arr;
            }
            if (c != ',') {
                throw error("期望 ',' 或 ']'，遇到 " + c);
            }
        }
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw error("字符串未闭合");
            }
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char esc = next();
                switch (esc) {
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
                            throw error("\\u 转义不完整");
                        }
                        String hex = src.substring(pos, pos + 4);
                        pos += 4;
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException ex) {
                            throw error("非法 \\u 转义: " + hex);
                        }
                    }
                    default -> throw error("非法转义: \\" + esc);
                }
            } else if (c < 0x20) {
                throw error("字符串中存在未转义控制字符");
            } else {
                sb.append(c);
            }
        }
    }

    private JsonValue readBool() {
        if (src.startsWith("true", pos)) {
            pos += 4;
            return JsonValue.of(true);
        }
        if (src.startsWith("false", pos)) {
            pos += 5;
            return JsonValue.of(false);
        }
        throw error("非法字面量");
    }

    private JsonValue readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return JsonValue.nul();
        }
        throw error("非法字面量");
    }

    private JsonValue.Num readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        readDigits();
        if (pos < src.length() && src.charAt(pos) == '.') {
            pos++;
            readDigits();
        }
        if (pos < src.length() && (src.charAt(pos) == 'e' || src.charAt(pos) == 'E')) {
            pos++;
            if (pos < src.length() && (src.charAt(pos) == '+' || src.charAt(pos) == '-')) {
                pos++;
            }
            readDigits();
        }
        String token = src.substring(start, pos);
        if (token.isEmpty() || "-".equals(token)) {
            throw error("非法数字");
        }
        try {
            return new JsonValue.Num(Double.parseDouble(token));
        } catch (NumberFormatException ex) {
            throw error("非法数字: " + token);
        }
    }

    private void readDigits() {
        int start = pos;
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            pos++;
        }
        if (pos == start) {
            throw error("期望数字");
        }
    }

    private void skipWs() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) {
            pos++;
        }
    }

    private char peek() {
        if (pos >= src.length()) {
            throw error("意外结束");
        }
        return src.charAt(pos);
    }

    private char next() {
        if (pos >= src.length()) {
            throw error("意外结束");
        }
        return src.charAt(pos++);
    }

    private void expect(char expected) {
        char actual = next();
        if (actual != expected) {
            throw error("期望 '" + expected + "'，遇到 '" + actual + "'");
        }
    }

    private JsonException error(String message) {
        return new JsonException(message + "（位置 " + pos + "）");
    }
}
