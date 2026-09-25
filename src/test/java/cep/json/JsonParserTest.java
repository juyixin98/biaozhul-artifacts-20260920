package cep.json;

import cep.test.TestFramework;

import java.util.List;

/** JSON 解析/写出的基础往返测试。 */
public final class JsonParserTest {

    private JsonParserTest() {}

    public static void register(TestFramework tf) {
        tf.addTest("JSON: 基本类型解析与取值", () -> {
            JsonValue.Obj o = JsonParser.parse(
                    "{\"s\":\"x\\ny\",\"n\":42,\"f\":1.5,\"b\":true,\"z\":null}").asObj();
            TestFramework.assertEquals("x\ny", o.requireString("s"), "字符串转义");
            TestFramework.assertEquals(42L, o.requireLong("n"), "整数");
            TestFramework.assertEquals(1.5d, o.require("f").asDouble(), "小数");
            TestFramework.assertTrue(o.requireBool("b"), "布尔");
            TestFramework.assertTrue(o.get("z") instanceof JsonValue.Null, "null");
        });

        tf.addTest("JSON: 数组/嵌套/空白", () -> {
            JsonValue.Arr a = JsonParser.parse(" [ 1, 2 , {\"k\": [true]} ] ").asArr();
            TestFramework.assertEquals(3, a.size(), "数组长度");
            TestFramework.assertTrue(a.get(2).asObj().get("k").asArr().get(0).asBool(),
                    "嵌套布尔");
        });

        tf.addTest("JSON: 中文/u转义往返", () -> {
            String text = "{\"msg\":\"按键事件\\u0041\"}";
            JsonValue.Obj o = JsonParser.parse(text).asObj();
            TestFramework.assertEquals("按键事件A", o.requireString("msg"), "unicode 转义");
            String again = JsonWriter.write(JsonParser.parse(JsonWriter.write(o)));
            TestFramework.assertTrue(again.contains("按键事件A"), "序列化往返: " + again);
        });

        tf.addTest("JSON: 非法输入抛 JsonException", () -> {
            for (String bad : List.of("", "{", "[1,]", "{\"a\":}", "tru", "\"unterminated")) {
                try {
                    JsonParser.parse(bad);
                    TestFramework.fail("应当解析失败: " + bad);
                } catch (JsonException expected) {
                    // 预期
                }
            }
        });
    }
}
