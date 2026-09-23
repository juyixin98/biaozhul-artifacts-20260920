package vecsearch.json;

import java.util.Collection;
import java.util.Map;

/** 最小 JSON 序列化器：只处理服务端自身构造的 Map/List/String/Number/Boolean/null。 */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            writeNumber(sb, n);
        } else if (value instanceof Map<?, ?> map) {
            writeObject(sb, map);
        } else if (value instanceof Collection<?> col) {
            writeArray(sb, col);
        } else if (value instanceof Object[] arr) {
            writeArrayElements(sb, java.util.Arrays.asList(arr));
        } else {
            // 未知类型按字符串处理，避免输出非法 JSON
            writeString(sb, value.toString());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
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

    private static void writeArray(StringBuilder sb, Collection<?> col) {
        sb.append('[');
        writeArrayElements(sb, col);
        sb.append(']');
    }

    private static void writeArrayElements(StringBuilder sb, Collection<?> col) {
        boolean first = true;
        for (Object o : col) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeTo(sb, o);
        }
    }

    private static void writeNumber(StringBuilder sb, Number n) {
        double d = n.doubleValue();
        if (Double.isNaN(d) || Double.isInfinite(d)) {
            // JSON 无法表达 NaN/Infinity，用 null 兜底
            sb.append("null");
            return;
        }
        if (n instanceof Double || n instanceof Float) {
            // 整数浮点输出为 1.0 没问题；直接用 Double.toString（保证可解析回 double）
            sb.append(n.toString());
        } else {
            sb.append(n.toString());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }
}
