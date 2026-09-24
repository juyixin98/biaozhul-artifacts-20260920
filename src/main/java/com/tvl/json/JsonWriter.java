package com.tvl.json;

import java.util.Collection;
import java.util.Map;

/** 极简 JSON 输出器，直接面向我们自己的响应结构，保证确定性行为。 */
public final class JsonWriter {

    private final StringBuilder sb = new StringBuilder();

    public static String write(Object value) {
        JsonWriter w = new JsonWriter();
        w.value(value);
        return w.sb.toString();
    }

    private void value(Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            string(s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Integer || v instanceof Long) {
            sb.append(v);
        } else if (v instanceof Double d) {
            if (!Double.isFinite(d)) {
                throw new JsonException("不能序列化非有限 DOUBLE");
            }
            sb.append(d);
        } else if (v instanceof Number n) {
            sb.append(n);
        } else if (v instanceof Map<?, ?> map) {
            object(map);
        } else if (v instanceof Collection<?> col) {
            array(col);
        } else if (v instanceof Object[] arr) {
            array(java.util.Arrays.asList(arr));
        } else {
            throw new JsonException("不支持的 JSON 输出类型: " + v.getClass());
        }
    }

    private void object(Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            string(String.valueOf(e.getKey()));
            sb.append(':');
            value(e.getValue());
        }
        sb.append('}');
    }

    private void array(Collection<?> col) {
        sb.append('[');
        boolean first = true;
        for (Object item : col) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            value(item);
        }
        sb.append(']');
    }

    private void string(String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
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
