package cep.json;

import java.util.Map;

/** JSON 写出器（紧凑格式 + 可选缩进美化）。 */
public final class JsonWriter {

    private final StringBuilder sb = new StringBuilder();
    private final String indent;

    private JsonWriter(String indent) {
        this.indent = indent;
    }

    public static String write(JsonValue value) {
        return write(value, null);
    }

    public static String pretty(JsonValue value) {
        return write(value, "  ");
    }

    public static String write(JsonValue value, String indent) {
        JsonWriter w = new JsonWriter(indent);
        w.writeValue(value == null ? JsonValue.nul() : value, 0);
        return w.sb.toString();
    }

    private void writeValue(JsonValue v, int depth) {
        switch (v) {
            case JsonValue.Null ignored -> sb.append("null");
            case JsonValue.Bool b -> sb.append(b.asBool());
            case JsonValue.Num n -> writeNumber(n.asDouble());
            case JsonValue.Str s -> writeString(s.asString());
            case JsonValue.Arr a -> writeArr(a, depth);
            case JsonValue.Obj o -> writeObj(o, depth);
        }
    }

    private void writeObj(JsonValue.Obj obj, int depth) {
        if (obj.entries().isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, JsonValue> e : obj.entries()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline(depth + 1);
            writeString(e.getKey());
            sb.append(indent == null ? ":" : ": ");
            writeValue(e.getValue(), depth + 1);
        }
        newline(depth);
        sb.append('}');
    }

    private void writeArr(JsonValue.Arr arr, int depth) {
        if (arr.size() == 0) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        boolean first = true;
        for (JsonValue v : arr.values()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            newline(depth + 1);
            writeValue(v, depth + 1);
        }
        newline(depth);
        sb.append(']');
    }

    private void writeNumber(double d) {
        if (Double.isNaN(d) || Double.isInfinite(d)) {
            throw new JsonException("JSON 不支持 NaN/Infinity");
        }
        if (d == Math.rint(d) && !Double.isInfinite(d)
                && d >= Long.MIN_VALUE && d <= Long.MAX_VALUE) {
            // 整数值（含超出 JS 安全整数范围的）按整数输出，避免科学计数法丢精度观感
            sb.append(Long.toString((long) d));
        } else {
            sb.append(Double.toString(d));
        }
    }

    private void writeString(String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
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

    private void newline(int depth) {
        if (indent != null) {
            sb.append('\n');
            sb.append(indent.repeat(depth));
        }
    }
}
