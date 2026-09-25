package com.example.streammatch.tests;

import com.example.streammatch.json.Json;

import java.util.List;
import java.util.Map;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/** JSON 解析/序列化往返：Unicode 转义、数字、嵌套、畸形输入拒绝。 */
public final class JsonTest {

    private JsonTest() {
    }

    public static void run() {
        section("JSON 模块", () -> {
            // 基本往返
            Object round = Json.parse(Json.write(Map.of(
                    "s", "he", "n", 42L, "b", true, "arr", List.of(1L, 2L, 3L))));
            assertTrue(round instanceof Map, "对象往返类型正确");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) round;
            assertEquals("he", m.get("s"), "字符串往返");
            assertEquals(42L, m.get("n"), "整数往返为 Long");
            assertEquals(Boolean.TRUE, m.get("b"), "布尔往返");
            assertEquals(List.of(1L, 2L, 3L), m.get("arr"), "数组往返");

            // Unicode 转义（含代理对）
            assertEquals("日本😀", Json.parse("\"\\u65e5\\u672c\\uD83D\\uDE00\""),
                    "\\uXXXX 与星面层代理对转义解析");
            assertEquals("a\"b\\c\n\t", Json.parse("\"a\\\"b\\\\c\\n\\t\""),
                    "特殊字符转义解析");

            // 非 ASCII 原样序列化（合法 UTF-8）
            assertTrue(Json.write(Map.of("x", "日本語")).contains("日本語"),
                    "非 ASCII 字符原样输出");

            // null / 浮点
            @SuppressWarnings("unchecked")
            Map<String, Object> nums = (Map<String, Object>) Json.parse(
                    "{\"a\":null,\"b\":-7,\"c\":1.5,\"d\":1e3}");
            assertEquals(null, nums.get("a"), "null 解析");
            assertEquals(-7L, nums.get("b"), "负整数");
            assertEquals(1.5, nums.get("c"), "小数");
            assertEquals(1000.0, nums.get("d"), "科学计数法");

            // 保序
            String order = "{\"z\":1,\"a\":2,\"m\":3}";
            @SuppressWarnings("unchecked")
            Map<String, Object> om = (Map<String, Object>) Json.parse(order);
            assertEquals(List.of("z", "a", "m"), List.copyOf(om.keySet()), "对象键保序");

            // pretty 输出可再解析
            Object reparsed = Json.parse(Json.writePretty(Map.of("k", List.of(1L, 2L))));
            assertTrue(reparsed instanceof Map, "pretty 输出可往返");

            // 畸形输入
            expectBad("{,}", "对象首键缺失");
            expectBad("[1,2,]", "数组尾逗号");
            expectBad("\"unterminated", "未闭合字符串");
            expectBad("{\"a\":1} extra", "尾部多余字符");
            expectBad("tru", "残缺字面量");
            expectBad("[\"x\\u12\"]", "残缺 \\u 转义");
            expectBad("\"控制字符\n\"", "未转义控制字符");
        });
    }

    private static void expectBad(String input, String label) {
        boolean threw = false;
        try {
            Json.parse(input);
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        assertTrue(threw, "畸形 JSON 被拒绝: " + label);
    }
}
