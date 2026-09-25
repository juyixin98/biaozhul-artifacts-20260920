package booleansearch;

import booleansearch.json.Json;
import booleansearch.json.JsonParseException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * JSON 解析/序列化往返测试，以及解析错误位置。
 */
public final class JsonTest {

    @SuppressWarnings("unchecked")
    public static void run() {
        TestFramework.reset();
        TestFramework.section("JsonTest: 解析与往返");

        Map<String, Object> original = new LinkedHashMap<>();
        original.put("query", "cat AND NOT dog");
        Map<String, Object> nested = new LinkedHashMap<>();
        nested.put("ids", List.of(1L, 2L, 3L));
        nested.put("ok", true);
        nested.put("empty", null);
        original.put("nested", nested);
        original.put("unicode", "中文标题");
        original.put("escaped", "引号\"换行\n反斜杠\\");
        String json = Json.stringify(original);
        Object parsed = Json.parse(json);
        assertEquals(original, parsed, "JSON 序列化后再解析应与原对象相等");
        assertTrue(Json.pretty(original).contains("\n"), "pretty 输出含换行缩进");
        assertTrue(Json.pretty(List.of()).trim().equals("[]"), "空数组 pretty 为单行");

        // record 必须序列化为 JSON 对象，而不是 toString() 字符串
        Point p = new Point(7, "x");
        String pointJson = Json.stringify(p);
        assertTrue(pointJson.contains("\"id\":7") && pointJson.contains("\"name\":\"x\""),
                "record 序列化为对象: " + pointJson);
        @SuppressWarnings("unchecked")
        Map<String, Object> pointBack = (Map<String, Object>) Json.parse(pointJson);
        assertEquals(7L, pointBack.get("id"), "record 数字字段往返保持 Long");
        assertEquals("x", pointBack.get("name"), "record 字符串字段往返");

        // 数字与字面量
        assertEquals(42L, ((Map<String, Object>) Json.parse("{\"v\":42}")).get("v"),
                "整数解析为 Long");
        assertEquals(1.5d, ((Map<String, Object>) Json.parse("{\"v\":1.5}")).get("v"),
                "小数解析为 Double");

        // 错误位置
        assertJsonError("{\"a\": }", 6);
        assertJsonError("{,}", 1);
        assertJsonError("[1, 2,]", 6);
        assertJsonError("{\"a\" 1}", 5);
        assertJsonError("tru", 0);
        assertJsonError("{\"a\":\"unterminated", 18);
        assertJsonError("{} extra", 3);

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("JsonTest 存在失败");
        }
    }

    private static void assertJsonError(String json, int expectedPosition) {
        try {
            Json.parse(json);
            assertTrue(false, "JSON \"" + json + "\" 应当解析失败");
        } catch (JsonParseException e) {
            assertEquals(expectedPosition, e.position(),
                    "JSON \"" + json + "\" 错误位置 -> " + e.getMessage());
        }
    }

    /** 用于验证 record 序列化的测试夹具。 */
    public record Point(int id, String name) {
    }
}
