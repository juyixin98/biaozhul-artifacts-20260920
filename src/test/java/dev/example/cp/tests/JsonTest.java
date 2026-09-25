package dev.example.cp.tests;

import dev.example.cp.json.Json;
import dev.example.cp.json.JsonException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class JsonTest extends TestCase {

    public JsonTest() {
        super("json/parse-and-write round trip");
    }

    @Override
    protected void run() {
        Object parsed = Json.parse("{\"a\":1,\"b\":[true,false,null,\"x\\\"y\",-7,2.5],\"c\":{}}");
        Map<?, ?> m = (Map<?, ?>) parsed;
        assertEquals(1L, ((Number) m.get("a")).longValue(), "a");
        List<?> b = (List<?>) m.get("b");
        assertEquals(Boolean.TRUE, b.get(0), "bool");
        assertEquals("x\"y", b.get(3), "escape");
        assertEquals(-7L, ((Number) b.get(4)).longValue(), "neg");
        assertEquals(2.5, ((Number) b.get(5)).doubleValue(), "frac");

        // 整数不应该被解析成浮点
        assertEquals(Long.class, Json.parse("42").getClass(), "long type");

        // 往返
        Map<String, Object> orig = new LinkedHashMap<>();
        orig.put("k", "v");
        orig.put("n", 3L);
        orig.put("arr", List.of(1L, 2L));
        String json = Json.write(orig);
        assertEquals(orig, Json.parse(json), "round trip");

        // 错误输入
        assertThrows(JsonException.class, () -> Json.parse("{bad}"), "reject bad object");
        assertThrows(JsonException.class, () -> Json.parse("[1,2,"), "reject truncated array");
        assertThrows(JsonException.class, () -> Json.parse("\"unterminated"), "reject unterminated string");
    }
}
