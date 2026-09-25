package sessions.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简递归下降 JSON 解析器，零外部依赖。
 *
 * <p>映射到的 Java 类型：
 * object -&gt; {@link LinkedHashMap}&lt;String,Object&gt;；array -&gt; {@link java.util.ArrayList}；
 * 整数 -&gt; {@link Long}；小数/指数 -&gt; {@link Double}；true/false -&gt; Boolean；null -&gt; null。
 * 字符串解析标准转义；拒绝尾随字符与非法空白外内容。
 */
public final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        Json p = new Json(text);
        p.skipWs();
        Object v = p.value();
        p.skipWs();
        if (p.i != p.s.length()) {
            throw new JsonException("trailing characters at position " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private Object value() {
        skipWs();
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        char c = s.charAt(i);
        return switch (c) {
            case '{' -> object();
            case '[' -> array();
            case '"' -> string();
            case 't', 'f' -> bool();
            case 'n' -> nul();
            default -> number();
        };
    }

    private Map<String, Object> object() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') {
            i++;
            return map;
        }
        while (true) {
            skipWs();
            String key = string();
            skipWs();
            expect(':');
            map.put(key, value());
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at position " + (i - 1));
            }
        }
    }

    private List<Object> array() {
        List<Object> list = new java.util.ArrayList<>();
        expect('[');
        skipWs();
        if (peek() == ']') {
            i++;
            return list;
        }
        while (true) {
            list.add(value());
            skipWs();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or ']' at position " + (i - 1));
            }
        }
    }

    private String string() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            char c = next();
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
                        if (i + 4 > s.length()) {
                            throw new JsonException("bad unicode escape");
                        }
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                    }
                    default -> throw new JsonException("bad escape \\" + e);
                }
            } else {
                if (c < 0x20) {
                    throw new JsonException("unescaped control character in string");
                }
                sb.append(c);
            }
        }
    }

    private Boolean bool() {
        if (s.startsWith("true", i)) {
            i += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", i)) {
            i += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("invalid literal at position " + i);
    }

    private Object nul() {
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        throw new JsonException("invalid literal at position " + i);
    }

    private Number number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        // JSON 整数部分不允许前导零：0 或 以非零数字开头
        char first = peek();
        if (first == '0') {
            i++;
        } else if (first >= '1' && first <= '9') {
            readDigits();
        } else {
            throw new JsonException("invalid number at position " + i);
        }
        boolean isFloating = false;
        if (peek() == '.') {
            isFloating = true;
            i++;
            readDigits();
        }
        if (peek() == 'e' || peek() == 'E') {
            isFloating = true;
            i++;
            if (peek() == '+' || peek() == '-') {
                i++;
            }
            readDigits();
        }
        String token = s.substring(start, i);
        if (token.isEmpty() || token.equals("-")) {
            throw new JsonException("invalid number at position " + start);
        }
        if (isFloating) {
            return Double.parseDouble(token);
        }
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException ex) {
            // 超出 long 的整数按 double 解析
            return Double.parseDouble(token);
        }
    }

    private void readDigits() {
        if (!Character.isDigit(peek())) {
            throw new JsonException("expected digit at position " + i);
        }
        while (Character.isDigit(peek())) {
            i++;
        }
    }

    private void skipWs() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }

    private char peek() {
        return i < s.length() ? s.charAt(i) : '\0';
    }

    private char next() {
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        return s.charAt(i++);
    }

    private void expect(char c) {
        char actual = next();
        if (actual != c) {
            throw new JsonException("expected '" + c + "' but got '" + actual
                    + "' at position " + (i - 1));
        }
    }
}
