package tvl.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖 JSON 解析器与序列化器。
 *
 * 解析结果只使用标准 Java 类型：
 *  Map(String,Object) / List(Object) / String / Double(仅语法允许) /
 *  Long / Boolean / null。
 *
 * 注意：查询引擎只接受 JSON 整数（解析为 Long）；遇到带小数点/指数的数
 * 会保留为 Double，但在装载 INTEGER 列时会被拒绝并报错。
 */
public final class Json {

    private Json() {}

    // ================= 解析 =================

    public static Object parse(String text) {
        Parser p = new Parser(text);
    p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonParseException("JSON 结束后仍有多余内容", p.line, p.col);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonParseException("期望 JSON 对象 { ... }", 1, 1);
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int i = 0;
        int line = 1, col = 1;

        Parser(String s) {
            this.s = s == null ? "" : s;
        }

        boolean eof() {
            return i >= s.length();
        }

        char peek() {
            if (i >= s.length()) {
                throw new JsonParseException("意外结束：JSON 输入不完整", line, col);
            }
            return s.charAt(i);
        }

        void skipWs() {
            while (!eof()) {
                char c = peek();
                if (c == ' ' || c == '\t' || c == '\r') {
                    advance();
                } else if (c == '\n') {
                    advance();
                } else {
                    break;
                }
            }
        }

        void advance() {
            if (!eof()) {
                if (s.charAt(i) == '\n') { line++; col = 1; } else { col++; }
                i++;
            }
        }

        Object readValue() {
            skipWs();
            if (eof()) {
                throw new JsonParseException("意外结束：期望一个 JSON 值", line, col);
            }
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': case 'f': return readBoolean();
                case 'n': return readNull();
                default:
                    if (c == '-' || c == '+') {
                        if (c == '+') {
                            throw new JsonParseException("数字不允许以 '+' 开头", line, col);
                        }
                    }
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return readNumber();
                    }
                    throw new JsonParseException("无法识别的 JSON 值，起始字符 '"
                            + c + "'", line, col);
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> m = new LinkedHashMap<>();
            skipWs();
            if (consume('}')) {
                return m;
            }
            while (true) {
                skipWs();
                if (peek() != '"') {
                    throw new JsonParseException("对象键必须是字符串", line, col);
                }
                String key = readString();
                skipWs();
                expect(':');
                Object val = readValue();
                m.put(key, val);
                skipWs();
                if (consume(',')) {
                    continue;
                }
                expect('}');
                return m;
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (consume(']')) {
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWs();
                if (consume(',')) {
                    continue;
                }
                expect(']');
                return list;
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (!eof()) {
                char c = peek();
                if (c == '"') {
                    advance();
                    return sb.toString();
                }
                if (c == '\\') {
                    advance();
                    if (eof()) {
                        throw new JsonParseException("未结束的转义序列", line, col);
                    }
                    char esc = peek();
                    switch (esc) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u': {
                            advance();
                            int code = 0;
                            for (int k = 0; k < 4; k++) {
                                if (eof()) {
                                    throw new JsonParseException("不完整的 \\u 转义", line, col);
                                }
                                char h = peek();
                                int d = hexDigit(h);
                                if (d < 0) {
                                    throw new JsonParseException("\\u 后需要 4 位十六进制数字", line, col);
                                }
                                code = code * 16 + d;
                                advance();
                            }
                            sb.append((char) code);
                            continue;
                        }
                        default:
                            throw new JsonParseException("非法转义字符 \\" + esc, line, col);
                    }
                    advance();
                } else {
                    sb.append(c);
                    advance();
                }
            }
            throw new JsonParseException("字符串未结束（缺少 \"）", line, col);
        }

        Boolean readBoolean() {
            if (matchLiteral("true")) {
                return Boolean.TRUE;
            }
            if (matchLiteral("false")) {
                return Boolean.FALSE;
            }
            throw new JsonParseException("非法的字面量（期望 true/false/null）", line, col);
        }

        Object readNull() {
            if (matchLiteral("null")) {
                return null;
            }
            throw new JsonParseException("非法的字面量（期望 null）", line, col);
        }

        boolean matchLiteral(String lit) {
            if (s.regionMatches(i, lit, 0, lit.length())) {
                for (int k = 0; k < lit.length(); k++) {
                    advance();
                }
                return true;
            }
            return false;
        }

        Object readNumber() {
            int startLine = line, startCol = col;
            int begin = i;
            boolean isDouble = false;
            if (peek() == '-') {
                advance();
            }
            readIntDigits();
            if (!eof() && peek() == '.') {
                isDouble = true;
                advance();
                readIntDigits();
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                isDouble = true;
                advance();
                if (!eof() && (peek() == '+' || peek() == '-')) {
                    advance();
                }
                readIntDigits();
            }
            String text = s.substring(begin, i);
            if (isDouble) {
                try {
                    return Double.valueOf(text);
                } catch (NumberFormatException ex) {
                    throw new JsonParseException("非法数字：" + text, startLine, startCol);
                }
            }
            try {
                return Long.valueOf(text);
            } catch (NumberFormatException ex) {
                throw new JsonParseException("整数超出范围：" + text, startLine, startCol);
            }
        }

        void readIntDigits() {
            if (eof() || !(peek() >= '0' && peek() <= '9')) {
                throw new JsonParseException("数字此处应有一位数字", line, col);
            }
            while (!eof() && peek() >= '0' && peek() <= '9') {
                advance();
            }
        }

        void expect(char c) {
            if (eof() || peek() != c) {
                throw new JsonParseException("期望字符 '" + c + "'"
                        + (eof() ? "，但输入已结束" : "，实际为 '" + peek() + "'"),
                        line, col);
            }
            advance();
        }

        boolean consume(char c) {
            if (!eof() && peek() == c) {
                advance();
                return true;
            }
            return false;
        }

        int hexDigit(char c) {
            if (c >= '0' && c <= '9') {
                return c - '0';
            }
            if (c >= 'a' && c <= 'f') {
                return c - 'a' + 10;
            }
            if (c >= 'A' && c <= 'F') {
                return c - 'A' + 10;
            }
            return -1;
        }
    }

    // ================= 序列化 =================

    /** 紧凑输出。 */
    public static String stringify(Object v) {
        StringBuilder sb = new StringBuilder();
        write(sb, v);
        return sb.toString();
    }

    /** 两空格缩进的美观输出。 */
    public static String pretty(Object v) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, v, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Number) {
            sb.append(v);
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) { sb.append(','); }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object o : (List<?>) v) {
                if (!first) { sb.append(','); }
                first = false;
                write(sb, o);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v == null || v instanceof Boolean || v instanceof Number) {
            sb.append(v == null ? "null" : String.valueOf(v));
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Map) {
            Map<?, ?> m = (Map<?, ?>) v;
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) { sb.append(",\n"); }
                first = false;
                pad(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append('}');
        } else if (v instanceof List) {
            List<?> list = (List<?>) v;
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            for (int k = 0; k < list.size(); k++) {
                if (k > 0) { sb.append(",\n"); }
                pad(sb, indent + 1);
                writePretty(sb, list.get(k), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
    }

    private static void pad(StringBuilder sb, int level) {
        for (int k = 0; k < level * 2; k++) {
            sb.append(' ');
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int k = 0; k < s.length(); k++) {
            char c = s.charAt(k);
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

    // ================= 便捷取值（带类型错误提示） =================

    public static String requireString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String)) {
            throw new BadRequestException("字段 '" + key + "' 必须是字符串");
        }
        return (String) v;
    }

    public static String optionalString(Map<String, Object> m, String key, String dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof String)) {
            throw new BadRequestException("字段 '" + key + "' 必须是字符串");
        }
        return (String) v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> requireObject(Object v, String what) {
        if (!(v instanceof Map)) {
            throw new BadRequestException(what + " 必须是 JSON 对象");
        }
        return (Map<String, Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> requireArray(Object v, String what) {
        if (!(v instanceof List)) {
            throw new BadRequestException(what + " 必须是 JSON 数组");
        }
        return (List<Object>) v;
    }
}
