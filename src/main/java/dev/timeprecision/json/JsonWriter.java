package dev.timeprecision.json;

import java.util.Map;

/** Serializes {@link JsonValue} trees to compact JSON text. */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(JsonValue value) {
        StringBuilder sb = new StringBuilder();
        writeValue(value, sb);
        return sb.toString();
    }

    private static void writeValue(JsonValue value, StringBuilder sb) {
        switch (value) {
            case JsonValue.Obj obj -> {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<String, JsonValue> entry : obj.members().entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeString(entry.getKey(), sb);
                    sb.append(':');
                    writeValue(entry.getValue(), sb);
                }
                sb.append('}');
            }
            case JsonValue.Arr arr -> {
                sb.append('[');
                boolean first = true;
                for (JsonValue item : arr.items()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeValue(item, sb);
                }
                sb.append(']');
            }
            case JsonValue.Str str -> writeString(str.value(), sb);
            case JsonValue.Num num -> sb.append(num.raw());
            case JsonValue.Bool bool -> sb.append(bool.value());
            case JsonValue.Null ignored -> sb.append("null");
        }
    }

    private static void writeString(String s, StringBuilder sb) {
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
}
