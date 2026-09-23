package dedup.tests;

import dedup.json.Json;

/** 手写 JSON 工具的解析 / 序列化 / 规范化哈希单测。 */
final class JsonTest {

    private JsonTest() {}

    static void register(TestFramework tf) {
        tf.run("JSON解析：嵌套对象/数组/字符串转义/指数数字", a -> {
            Json.Value v = Json.parse("""
                    {"a":[1, 2.5, -3, 1e3, "x\\t\\"y"], "b":true, "c":null, "d":{"k":"v"}}
                    """);
            a.check(v instanceof Json.Obj, "根是对象");
            Json.Obj o = (Json.Obj) v;
            a.check(o.get("b") instanceof Json.Bool b && b.value(), "b=true");
            a.check(o.get("c") == Json.Nul.INSTANCE, "c=null");
            Json.Arr arr = (Json.Arr) o.get("a");
            a.eq(((Json.Num) arr.get(0)).value().intValue(), 1, "整数 1");
            a.eq(((Json.Num) arr.get(3)).value().intValue(), 1000, "指数 1e3=1000");
            a.eq(((Json.Str) arr.get(4)).value(), "x\t\"y", "转义字符");
            a.eq(((Json.Obj) o.get("d")).getStr("k"), "v", "嵌套对象");
        });

        tf.run("JSON解析：非法输入抛出JsonException", a -> {
            a.throws_(Json.JsonException.class, () -> Json.parse(""), "空串");
            a.throws_(Json.JsonException.class, () -> Json.parse("{"), "未闭合对象");
            a.throws_(Json.JsonException.class, () -> Json.parse("[1,]"), "数组尾逗号");
            a.throws_(Json.JsonException.class, () -> Json.parse("123abc"), "数字尾巴");
            a.throws_(Json.JsonException.class, () -> Json.parse("\"unterminated"),
                    "未闭合字符串");
            a.throws_(Json.JsonException.class, () -> Json.parse("{\"a\":1}extra"),
                    "根元素后多余字符");
        });

        tf.run("规范化：键序不同与数字写法不同的载荷哈希一致", a -> {
            String h1 = Json.sha256Canonical(Json.parse("{\"a\":1,\"b\":2}"));
            String h2 = Json.sha256Canonical(Json.parse("{\"b\":2,\"a\":1.0}"));
            a.eq(h1, h2, "键序/1 与 1.0 必须规范化为相同哈希");

            String h3 = Json.sha256Canonical(Json.parse("{\"a\":1,\"b\":2}"));
            String h4 = Json.sha256Canonical(Json.parse("{\"a\":1,\"b\":3}"));
            a.check(!h3.equals(h4), "值不同哈希必须不同");

            String h5 = Json.sha256Canonical(Json.parse("{\"a\":[1,2]}"));
            String h6 = Json.sha256Canonical(Json.parse("{\"a\":[2,1]}"));
            a.check(!h5.equals(h6), "数组是有序的：[1,2] 与 [2,1] 不同");
        });

        tf.run("序列化往返：parse(write(x)) 保持结构", a -> {
            Json.Value original = Json.parse(
                    "{\"id\":\"e\",\"eventTime\":1000,\"payload\":{\"n\":1,\"arr\":[true,null]}}");
            Json.Value roundTrip = Json.parse(Json.write(original));
            a.eq(Json.canonical(roundTrip), Json.canonical(original),
                    "往返后规范化表示必须一致");
        });
    }
}
