package sessions;

import java.util.List;
import java.util.Map;

import sessions.json.Json;
import sessions.json.JsonException;
import sessions.json.JsonWriter;
import sessions.testing.Assert;
import sessions.testing.Test;

/** 极简 JSON 解析器/生成器测试。 */
public final class JsonTest {

    @Test("解析标量类型")
    static void scalars() {
        Assert.assertEquals(123L, ((Number) Json.parse("123")).longValue(), "long");
        Assert.assertEquals(-123L, ((Number) Json.parse("-123")).longValue(), "negative long");
        Assert.assertEquals(1.5d, ((Number) Json.parse("1.5")).doubleValue(), "double");
        Assert.assertEquals(Boolean.TRUE, Json.parse("true"), "true");
        Assert.assertEquals(Boolean.FALSE, Json.parse("false"), "false");
        Assert.assertEquals(null, Json.parse("null"), "null");
        Assert.assertEquals("hello", Json.parse("\"hello\""), "string");
    }

    @Test("解析对象与数组并保留插入顺序")
    static void objectsAndArrays() {
        Map<String, Object> m = (Map<String, Object>) Json.parse(
                "{\"b\": 1, \"a\": [true, null, \"x\"], \"c\": {\"d\": -2}}");
        Assert.assertEquals(1L, m.get("b"), "field b");
        Assert.assertEquals(Boolean.TRUE, ((List<?>) m.get("a")).get(0), "array[0]");
        Assert.assertEquals(null, ((List<?>) m.get("a")).get(1), "array[1] null");
        Assert.assertEquals("x", ((List<?>) m.get("a")).get(2), "array[2]");
        Assert.assertEquals(-2L, ((Map<?, ?>) m.get("c")).get("d"), "nested");
        Assert.assertEquals(List.of("b", "a", "c"), List.copyOf(m.keySet()),
                "insertion order preserved");
    }

    @Test("字符串转义往返")
    static void escapesRoundTrip() {
        String raw = "a\"b\\c\n d\t e/r";
        String encoded = JsonWriter.write(raw);
        Assert.assertEquals(raw, Json.parse(encoded), "escape round trip");
    }

    @Test("数字与结构往返")
    @SuppressWarnings("unchecked")
    static void structureRoundTrip() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("gap", 10L);
        m.put("names", List.of("a", "b"));
        Map<String, Object> nested = new java.util.LinkedHashMap<>();
        nested.put("x", 1L);
        nested.put("y", List.of(2L, 3L));
        m.put("nested", nested);
        String json = JsonWriter.write(m);
        Map<String, Object> reparsed = (Map<String, Object>) Json.parse(json);
        Assert.assertEquals(m.get("gap"), reparsed.get("gap"), "scalar field");
        Assert.assertEquals(m.get("names"), reparsed.get("names"), "array field");
        Assert.assertEquals(nested, reparsed.get("nested"), "nested map preserved");
    }

    @Test("非法 JSON 抛 JsonException")
    static void invalidInput() {
        String[] bad = {"", "{", "[1,]", "{\"a\"}", "tru", "01", "[1 2]", "{,}"};
        for (String b : bad) {
            try {
                Json.parse(b);
                Assert.fail("expected parse failure for: " + b);
            } catch (JsonException expected) {
                // expected
            }
        }
    }

    @Test("尾随字符被拒绝")
    static void trailingRejected() {
        try {
            Json.parse("{}garbage");
            Assert.fail("trailing chars must be rejected");
        } catch (JsonException expected) {
            // expected
        }
    }
}
