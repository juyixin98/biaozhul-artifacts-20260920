package com.example.sessionwindow.tests;

import com.example.sessionwindow.json.Json;
import com.example.sessionwindow.json.JsonException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertThrows;
import static com.example.sessionwindow.tests.Assert.assertTrue;

public class JsonTest {

    @Test
    void roundTripsNestedStructure() {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("name", "u1");
        root.put("timestamp", 42L);
        root.put("ratio", 2.5);
        root.put("ok", true);
        root.put("nothing", null);
        root.put("items", List.of(1L, 2L, 3L));
        Map<String, Object> nested = new LinkedHashMap<>();
        nested.put("k", "v");
        root.put("nested", nested);

        String json = Json.stringify(root);
        Map<String, Object> parsed = Json.parseObject(json);

        assertEquals("u1", parsed.get("name"), "string");
        assertEquals(42L, parsed.get("timestamp"), "long");
        assertEquals(2.5, parsed.get("ratio"), "double");
        assertEquals(Boolean.TRUE, parsed.get("ok"), "boolean");
        assertTrue(parsed.containsKey("nothing") && parsed.get("nothing") == null, "null");
        assertEquals(List.of(1L, 2L, 3L), parsed.get("items"), "array");
        assertEquals("v", ((Map<?, ?>) parsed.get("nested")).get("k"), "nested");
    }

    @Test
    void parsesEscapesAndWhitespace() {
        Map<String, Object> m = Json.parseObject("  { \"a\" : \"x\\ny\\t\\\"\" , \"b\" : -7 } ");
        assertEquals("x\ny\t\"", m.get("a"), "escapes decoded");
        assertEquals(-7L, m.get("b"), "negative integer");
    }

    @Test
    void rejectsMalformedInput() {
        assertThrows(JsonException.class, () -> Json.parse("{]"), "bad object");
        assertThrows(JsonException.class, () -> Json.parse("{\"a\":1} trailing"), "trailing chars");
        assertThrows(JsonException.class, () -> Json.parse("[1,2,]"), "trailing comma");
        assertThrows(JsonException.class, () -> Json.parseObject("[1,2]"), "array as object");
    }

    @Test
    void integerValuesParseAsLongAndFloatsAsDouble() {
        Map<String, Object> m = Json.parseObject("{\"i\":10,\"d\":10.0,\"e\":1e3}");
        assertEquals(Long.class, m.get("i").getClass(), "integer is Long");
        assertEquals(Double.class, m.get("d").getClass(), "decimal is Double");
        assertEquals(1000.0, m.get("e"), "exponent parsed");
    }
}
