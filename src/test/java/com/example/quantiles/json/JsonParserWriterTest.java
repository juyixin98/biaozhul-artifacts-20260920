package com.example.quantiles.json;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonParserWriterTest {

    @Test
    void parsesNestedStructureWithLongsAndStrings() {
        Object v = JsonParser.parse("""
                {"windowSizeMillis": 1000, "events": [
                  {"timestampMillis": -15, "value": 0},
                  {"timestampMillis": 2, "value": 9223372036854775807}
                ], "quantiles": [0.5, 0.9], "name": "中 文\\n"}
                """);
        Map<String, Object> root = Json.asObject(v);
        assertEquals(1000L, Json.asLong(root.get("windowSizeMillis")));
        List<Object> events = Json.asArray(root.get("events"));
        assertEquals(-15L, Json.asLong(Json.asObject(events.get(0)).get("timestampMillis")));
        assertEquals(Long.MAX_VALUE,
                Json.asLong(Json.asObject(events.get(1)).get("value")));
        assertEquals(0.5, Json.asDouble(Json.asArray(root.get("quantiles")).get(0)));
        assertEquals("中 文\n", Json.asString(root.get("name")));
    }

    @Test
    void longIntegersDoNotLosePrecision() {
        Object v = JsonParser.parse("{\"x\": 9223372036854775807}");
        assertEquals(Long.MAX_VALUE, Json.asLong(Json.asObject(v).get("x")));
    }

    @Test
    void rejectsMalformedJson() {
        for (String bad : List.of("", "{", "[", "{\"a\":}", "[1,]", "{'a':1}",
                "{a:1}", "1,2", "tru", "null x")) {
            assertThrows(JsonException.class, () -> JsonParser.parse(bad),
                    "应当拒绝: " + bad);
        }
    }

    @Test
    void rejectsTrailingCommaAndPlus() {
        assertThrows(JsonException.class, () -> JsonParser.parse("{\"a\":1,}"));
        assertThrows(JsonException.class, () -> JsonParser.parse("[+1]"));
    }

    @Test
    void roundTripsBuiltValues() {
        Map<String, Object> root = new java.util.LinkedHashMap<>();
        root.put("a", 1L);
        root.put("b", List.of(1L, 2L, 3L));
        root.put("c", "hi");
        root.put("d", null);
        root.put("e", true);
        String compact = JsonWriter.write(root);
        Object reparsed = JsonParser.parse(compact);
        Map<String, Object> back = Json.asObject(reparsed);
        assertEquals(1L, Json.asLong(back.get("a")));
        assertEquals(3, Json.asArray(back.get("b")).size());
        assertEquals("hi", Json.asString(back.get("c")));
        assertTrue(Json.isNull(back.get("d")));
        assertEquals(true, Json.asBool(back.get("e")));
    }

    @Test
    void prettyWriterIndentsAndEscapes() {
        String out = JsonWriter.writePretty(Map.of("s", "a\nb"));
        assertTrue(out.contains("\n"), out);
        assertTrue(out.contains("\\n"), out);
    }

    @Test
    void asLongRejectsFractionalNumbers() {
        Object v = JsonParser.parse("{\"x\": 1.5}");
        assertThrows(JsonException.class,
                () -> Json.asLong(Json.asObject(v).get("x")));
    }
}
