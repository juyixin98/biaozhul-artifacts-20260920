package engine.json;

import java.util.List;
import java.util.Map;

/** JSON 序列化器：紧凑输出或两空格缩进输出。 */
public final class JsonWriter {

    private JsonWriter() {
    }

    public static String write(Json value) {
        return write(value, true);
    }

    public static String write(Json value, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        append(sb, value, pretty, 0);
        return sb.toString();
    }

    private static void append(StringBuilder sb, Json v, boolean pretty, int depth) {
        if (v instanceof Json.JNull) {
            sb.append("null");
        } else if (v instanceof Json.JBool b) {
            sb.append(b.value());
        } else if (v instanceof Json.JLong n) {
            sb.append(n.value());
        } else if (v instanceof Json.JDouble d) {
            sb.append(d.value());
        } else if (v instanceof Json.JStr s) {
            quote(sb, s.value());
        } else if (v instanceof Json.JArr a) {
            if (a.items.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            for (int i = 0; i < a.items.size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                if (pretty) {
                    sb.append('\n').append("  ".repeat(depth + 1));
                }
                append(sb, a.items.get(i), pretty, depth + 1);
            }
            if (pretty) {
                sb.append('\n').append("  ".repeat(depth));
            }
            sb.append(']');
        } else if (v instanceof Json.JObj o) {
            if (o.members.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append('{');
            int i = 0;
            for (Map.Entry<String, Json> e : o.members.entrySet()) {
                if (i++ > 0) {
                    sb.append(',');
                }
                if (pretty) {
                    sb.append('\n').append("  ".repeat(depth + 1));
                }
                quote(sb, e.getKey());
                sb.append(pretty ? ": " : ":");
                append(sb, e.getValue(), pretty, depth + 1);
            }
            if (pretty) {
                sb.append('\n').append("  ".repeat(depth));
            }
            sb.append('}');
        } else {
            throw new JsonException("无法序列化未知的 JSON 值类型: " + v.getClass());
        }
    }

    private static void quote(StringBuilder sb, String s) {
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

    // ---- 便于构造 JSON 的小工具 ----

    public static Json.JObj obj(Object... kv) {
        if (kv.length % 2 != 0) {
            throw new IllegalArgumentException("键值必须成对出现");
        }
        Json.JObj o = new Json.JObj();
        for (int i = 0; i < kv.length; i += 2) {
            o.put((String) kv[i], of(kv[i + 1]));
        }
        return o;
    }

    public static Json.JArr arr(List<? extends Json> items) {
        Json.JArr a = new Json.JArr();
        a.items.addAll(items);
        return a;
    }

    public static Json of(Object value) {
        if (value == null) {
            return Json.JNull.V;
        }
        if (value instanceof Json v) {
            return v;
        }
        if (value instanceof Boolean b) {
            return new Json.JBool(b);
        }
        if (value instanceof Long n) {
            return new Json.JLong(n);
        }
        if (value instanceof Integer n) {
            return new Json.JLong(n.longValue());
        }
        if (value instanceof Double d) {
            return new Json.JDouble(d);
        }
        if (value instanceof String str) {
            return new Json.JStr(str);
        }
        throw new JsonException("不支持的 JSON 值类型: " + value.getClass());
    }
}
