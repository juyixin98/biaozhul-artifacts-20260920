package engine.json;

/**
 * 手写递归下降 JSON 解析器（不依赖任何第三方库）。
 * 支持：空白、字符串转义（含 uXXXX 形式的 Unicode 转义与代理对）、true/false/null、
 * 整数（含负号）、浮点（含指数）、数组、对象。
 * 错误信息携带列位置，便于定位请求中的问题。
 */
public final class JsonParser {

    private final String s;
    private int pos;

    private JsonParser(String s) {
        this.s = s;
    }

    public static Json parse(String text) {
        JsonParser p = new JsonParser(text);
        p.ws();
        Json v = p.value();
        p.ws();
        if (p.pos != p.s.length()) {
            throw new JsonException("JSON 解析失败：位置 " + (p.pos + 1) + " 之后存在多余字符");
        }
        return v;
    }

    private Json value() {
        ws();
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 期望一个值，但已结束");
        }
        char c = s.charAt(pos);
        return switch (c) {
            case '{' -> object();
            case '[' -> array();
            case '"' -> new Json.JStr(string());
            case 't' -> literal("true", new Json.JBool(true));
            case 'f' -> literal("false", new Json.JBool(false));
            case 'n' -> literal("null", Json.JNull.V);
            default -> {
                if (c == '-' || (c >= '0' && c <= '9')) {
                    yield number();
                }
                throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 出现无法识别的字符 '" + c + "'");
            }
        };
    }

    private Json literal(String word, Json token) {
        if (!s.startsWith(word, pos)) {
            throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 期望字面量 " + word);
        }
        pos += word.length();
        return token;
    }

    private Json.JObj object() {
        Json.JObj o = new Json.JObj();
        expect('{');
        ws();
        if (peek() == '}') {
            pos++;
            return o;
        }
        while (true) {
            ws();
            if (peek() != '"') {
                throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 对象成员名必须是字符串");
            }
            String key = string();
            ws();
            expect(':');
            o.put(key, value());
            ws();
            char c = next();
            if (c == '}') {
                break;
            }
            if (c != ',') {
                throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 对象成员之间应为 ','，实际为 '" + c + "'");
            }
        }
        return o;
    }

    private Json.JArr array() {
        Json.JArr a = new Json.JArr();
        expect('[');
        ws();
        if (peek() == ']') {
            pos++;
            return a;
        }
        while (true) {
            a.add(value());
            ws();
            char c = next();
            if (c == ']') {
                break;
            }
            if (c != ',') {
                throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 数组元素之间应为 ','，实际为 '" + c + "'");
            }
        }
        return a;
    }

    private Json number() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：负号后缺少数字");
        }
        if (s.charAt(pos) == '0') {
            pos++;
        } else if (s.charAt(pos) >= '1' && s.charAt(pos) <= '9') {
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
        } else {
            throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 数字格式错误");
        }
        boolean isDouble = false;
        if (pos < s.length() && s.charAt(pos) == '.') {
            isDouble = true;
            pos++;
            int fracStart = pos;
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
            if (pos == fracStart) {
                throw new JsonException("JSON 解析失败：小数点后缺少数字");
            }
        }
        if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
            isDouble = true;
            pos++;
            if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                pos++;
            }
            int expStart = pos;
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
            if (pos == expStart) {
                throw new JsonException("JSON 解析失败：指数部分缺少数字");
            }
        }
        String lexeme = s.substring(start, pos);
        if (!isDouble) {
            try {
                return new Json.JLong(Long.parseLong(lexeme));
            } catch (NumberFormatException e) {
                // 超出 long 范围的整数：本引擎的数值列不支持，按双精度解析以便报错信息可读
                return new Json.JDouble(Double.parseDouble(lexeme));
            }
        }
        return new Json.JDouble(Double.parseDouble(lexeme));
    }

    private String string() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) {
                throw new JsonException("JSON 解析失败：字符串没有闭合的引号");
            }
            char c = s.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c < 0x20) {
                throw new JsonException("JSON 解析失败：字符串中出现未转义的控制字符 U+"
                        + Integer.toHexString(c).toUpperCase());
            }
            if (c == '\\') {
                if (pos >= s.length()) {
                    throw new JsonException("JSON 解析失败：反斜杠后缺少转义内容");
                }
                char e = s.charAt(pos++);
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
                        int hi = hex4();
                        if (Character.isHighSurrogate((char) hi)) {
                            if (pos + 1 < s.length() && s.charAt(pos) == '\\' && s.charAt(pos + 1) == 'u') {
                                pos += 2;
                                int lo = hex4();
                                if (Character.isLowSurrogate((char) lo)) {
                                    sb.append((char) hi).append((char) lo);
                                } else {
                                    throw new JsonException("JSON 解析失败：高位代理后不是合法的低位代理");
                                }
                            } else {
                                throw new JsonException("JSON 解析失败：高位代理缺少配对的低位 \\uXXXX");
                            }
                        } else {
                            sb.append((char) hi);
                        }
                    }
                    default -> throw new JsonException("JSON 解析失败：位置 " + pos + " 不支持的转义 '\\" + e + "'");
                }
            } else {
                sb.append(c);
            }
        }
    }

    private int hex4() {
        if (pos + 4 > s.length()) {
            throw new JsonException("JSON 解析失败：\\u 后需要 4 个十六进制数字");
        }
        int value = 0;
        for (int i = 0; i < 4; i++) {
            char c = s.charAt(pos++);
            value <<= 4;
            if (c >= '0' && c <= '9') {
                value += c - '0';
            } else if (c >= 'a' && c <= 'f') {
                value += c - 'a' + 10;
            } else if (c >= 'A' && c <= 'F') {
                value += c - 'A' + 10;
            } else {
                throw new JsonException("JSON 解析失败：位置 " + pos + " 不是十六进制数字 '" + c + "'");
            }
        }
        return value;
    }

    private void ws() {
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }

    private void expect(char c) {
        if (pos >= s.length() || s.charAt(pos) != c) {
            throw new JsonException("JSON 解析失败：位置 " + (pos + 1) + " 期望 '" + c + "'");
        }
        pos++;
    }

    private char peek() {
        return pos < s.length() ? s.charAt(pos) : '\0';
    }

    private char next() {
        if (pos >= s.length()) {
            throw new JsonException("JSON 解析失败：输入提前结束");
        }
        return s.charAt(pos++);
    }
}
