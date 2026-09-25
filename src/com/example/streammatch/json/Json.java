package com.example.streammatch.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析与序列化（零依赖，仅覆盖本服务需要的子集）。
 *
 * <p>解析结果使用标准 JDK 类型：</p>
 * <ul>
 *   <li>对象 → {@link LinkedHashMap}（保序）</li>
 *   <li>数组 → {@link ArrayList}</li>
 *   <li>字符串 → {@link String}（完整支持反斜杠 u+4 位十六进制转义与代理对转义）</li>
 *   <li>数字 → 能整除且在 long 范围内时为 {@link Long}，否则 {@link Double}</li>
 *   <li>true/false → {@link Boolean}；null → Java {@code null}</li>
 * </ul>
 */
public final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String input) {
        Json j = new Json(input);
        j.ws();
        Object v = j.value();
        j.ws();
        if (j.i != input.length()) {
            throw j.error("JSON 解析结束后仍有多余字符");
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String input) {
        Object v = parse(input);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("期望 JSON 对象，得到: " + typeName(v));
        }
        return (Map<String, Object>) v;
    }

    /** 序列化为紧凑 JSON（字符串做完整转义，非 ASCII 字符原样输出，合法 UTF-8）。 */
    public static String write(Object o) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, o);
        return sb.toString();
    }

    /** 序列化为带 2 空格缩进的 JSON，便于人读样例。 */
    public static String writePretty(Object o) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, o, 0);
        sb.append('\n');
        return sb.toString();
    }

    // ---------------------------------------------------------------- parsing

    private Object value() {
        ws();
        if (i >= s.length()) {
            throw error("意外的输入结尾");
        }
        char c = s.charAt(i);
        return switch (c) {
            case '{' -> object();
            case '[' -> array();
            case '"' -> string();
            case 't', 'f' -> bool();
            case 'n' -> nul();
            default -> {
                if (c == '-' || (c >= '0' && c <= '9')) {
                    yield number();
                }
                throw error("意外字符 '" + c + "'");
            }
        };
    }

    private Map<String, Object> object() {
        Map<String, Object> m = new LinkedHashMap<>();
        expect('{');
        ws();
        if (peek() == '}') {
            i++;
            return m;
        }
        while (true) {
            ws();
            String key = string();
            ws();
            expect(':');
            Object v = value();
            m.put(key, v);
            ws();
            char c = next();
            if (c == '}') {
                return m;
            }
            if (c != ',') {
                throw error("对象中期望 ',' 或 '}'，得到 '" + c + "'");
            }
        }
    }

    private List<Object> array() {
        List<Object> list = new ArrayList<>();
        expect('[');
        ws();
        if (peek() == ']') {
            i++;
            return list;
        }
        while (true) {
            list.add(value());
            ws();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw error("数组中期望 ',' 或 ']'，得到 '" + c + "'");
            }
        }
    }

    private String string() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (i >= s.length()) {
                throw error("字符串未闭合");
            }
            char c = s.charAt(i++);
            if (c == '"') {
                return sb.toString();
            }
            if (c < 0x20) {
                throw error("字符串中出现未转义控制字符 U+" + Integer.toHexString(c));
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
                        int cp = hex4();
                        if (Character.isHighSurrogate((char) cp) && i + 6 <= s.length()
                                && s.charAt(i) == '\\' && s.charAt(i + 1) == 'u') {
                            int mark = i;
                            i += 2;
                            int lo = hex4();
                            if (Character.isLowSurrogate((char) lo)) {
                                cp = Character.toCodePoint((char) cp, (char) lo);
                                sb.appendCodePoint(cp);
                            } else {
                                i = mark; // 不是代理对，按 BMP 字符处理
                                sb.append((char) cp);
                            }
                        } else {
                            sb.append((char) cp);
                        }
                    }
                    default -> throw error("非法转义 '\\" + e + "'");
                }
            } else {
                sb.append(c);
            }
        }
    }

    private int hex4() {
        if (i + 4 > s.length()) {
            throw error("\\u 转义不足 4 位十六进制");
        }
        int v = 0;
        for (int k = 0; k < 4; k++) {
            char c = s.charAt(i++);
            int d = Character.digit(c, 16);
            if (d < 0) {
                throw error("\\u 转义中出现非十六进制字符 '" + c + "'");
            }
            v = (v << 4) | d;
        }
        return v;
    }

    private Object number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        while (i < s.length()) {
            char c = s.charAt(i);
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                i++;
            } else {
                break;
            }
        }
        String tok = s.substring(start, i);
        if (tok.contains(".") || tok.contains("e") || tok.contains("E")) {
            return Double.parseDouble(tok);
        }
        try {
            return Long.parseLong(tok);
        } catch (NumberFormatException ex) {
            return Double.parseDouble(tok);
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
        throw error("非法字面量");
    }

    private Object nul() {
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        throw error("非法字面量");
    }

    private void ws() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }

    private char peek() {
        return i < s.length() ? s.charAt(i) : '\0';
    }

    private char next() {
        if (i >= s.length()) {
            throw error("意外的输入结尾");
        }
        return s.charAt(i++);
    }

    private void expect(char c) {
        if (i >= s.length() || s.charAt(i) != c) {
            throw error("期望 '" + c + "'");
        }
        i++;
    }

    private IllegalArgumentException error(String msg) {
        return new IllegalArgumentException("JSON 解析错误（位置 " + i + "）: " + msg);
    }

    // ---------------------------------------------------------------- writing

    private static void writeTo(StringBuilder sb, Object o) {
        switch (o) {
            case null -> sb.append("null");
            case Boolean b -> sb.append(b.booleanValue());
            case Number n -> sb.append(n.toString());
            case String str -> writeString(sb, str);
            case Map<?, ?> m -> {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : m.entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeString(sb, String.valueOf(e.getKey()));
                    sb.append(':');
                    writeTo(sb, e.getValue());
                }
                sb.append('}');
            }
            case List<?> list -> {
                sb.append('[');
                boolean first = true;
                for (Object v : list) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeTo(sb, v);
                }
                sb.append(']');
            }
            case Object[] arr -> {
                sb.append('[');
                for (int k = 0; k < arr.length; k++) {
                    if (k > 0) {
                        sb.append(',');
                    }
                    writeTo(sb, arr[k]);
                }
                sb.append(']');
            }
            default -> writeString(sb, o.toString());
        }
    }

    private static void writePretty(StringBuilder sb, Object o, int indent) {
        switch (o) {
            case Map<?, ?> m when m.isEmpty() -> sb.append("{}");
            case List<?> l when l.isEmpty() -> sb.append("[]");
            case Map<?, ?> m -> {
                sb.append("{\n");
                boolean first = true;
                for (Map.Entry<?, ?> e : m.entrySet()) {
                    if (!first) {
                        sb.append(",\n");
                    }
                    first = false;
                    pad(sb, indent + 1);
                    writeString(sb, String.valueOf(e.getKey()));
                    sb.append(": ");
                    writePretty(sb, e.getValue(), indent + 1);
                }
                sb.append('\n');
                pad(sb, indent);
                sb.append('}');
            }
            case List<?> list -> {
                sb.append("[\n");
                for (int k = 0; k < list.size(); k++) {
                    if (k > 0) {
                        sb.append(",\n");
                    }
                    pad(sb, indent + 1);
                    writePretty(sb, list.get(k), indent + 1);
                }
                sb.append('\n');
                pad(sb, indent);
                sb.append(']');
            }
            default -> writeTo(sb, o);
        }
    }

    private static void pad(StringBuilder sb, int levels) {
        sb.append("  ".repeat(levels));
    }

    private static void writeString(StringBuilder sb, String str) {
        sb.append('"');
        for (int i = 0; i < str.length(); ) {
            int cp = str.codePointAt(i);
            i += Character.charCount(cp);
            switch (cp) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (cp < 0x20) {
                        sb.append(String.format("\\u%04x", cp));
                    } else {
                        sb.appendCodePoint(cp);
                    }
                }
            }
        }
        sb.append('"');
    }

    private static String typeName(Object v) {
        return v == null ? "null" : v.getClass().getSimpleName();
    }

    // ------------------------------------------------------- typed accessors

    public static String getString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String str)) {
            throw new IllegalArgumentException("字段 '" + key + "' 需要字符串，得到: " + typeName(v));
        }
        return str;
    }

    public static String getStringOrDefault(Map<String, Object> m, String key, String def) {
        Object v = m.get(key);
        return v == null ? def : v instanceof String str ? str : String.valueOf(v);
    }

    public static int getIntOrDefault(Map<String, Object> m, String key, int def) {
        Object v = m.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        return Integer.parseInt(String.valueOf(v));
    }

    public static boolean getBoolOrDefault(Map<String, Object> m, String key, boolean def) {
        Object v = m.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Boolean b) {
            return b;
        }
        return Boolean.parseBoolean(String.valueOf(v));
    }

    @SuppressWarnings("unchecked")
    public static List<Object> getArray(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof List)) {
            throw new IllegalArgumentException("字段 '" + key + "' 需要数组，得到: " + typeName(v));
        }
        return (List<Object>) v;
    }
}
