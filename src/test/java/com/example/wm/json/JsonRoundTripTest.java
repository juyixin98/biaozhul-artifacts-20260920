package com.example.wm.json;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonRoundTripTest {

    @Test
    void parsesNestedStructure() {
        String text = """
                {
                  "boundMs": 100,
                  "idleTimeoutMs": 500,
                  "actions": [
                    {"type": "event", "partition": "a", "eventTime": 1000, "payload": "x"},
                    {"type": "advance", "durationMs": 250},
                    {"type": "tick"}
                  ],
                  "flag": true, "ratio": 1.5, "nothing": null
                }
                """;
        Map<String, Object> m = Json.parseObject(text);
        assertEquals(100L, m.get("boundMs"));
        assertEquals(500L, m.get("idleTimeoutMs"));
        assertEquals(Boolean.TRUE, m.get("flag"));
        assertEquals(1.5, m.get("ratio"));
        assertNull(m.get("nothing"));
        @SuppressWarnings("unchecked")
        List<Object> actions = (List<Object>) m.get("actions");
        assertEquals(3, actions.size());
        assertEquals("a", ((Map<?, ?>) actions.get(0)).get("partition"));
    }

    @Test
    void writerRoundTrips() {
        Map<String, Object> m = Map.of(
                "a", 1L, "b", "he\tllo\n", "c", List.of(1L, 2L), "d", Map.of("x", true));
        String json = JsonWriter.write(m);
        assertEquals(m, Json.parse(json));
    }

    @Test
    void prettyContainsNewlines() {
        String pretty = JsonWriter.pretty(Map.of("k", 1L));
        assertTrue(pretty.contains("\n"));
    }

    @Test
    void rejectsMalformedInput() {
        assertThrows(JsonException.class, () -> Json.parse("{\"a\":}"));
        assertThrows(JsonException.class, () -> Json.parse("[1,2,]"));
        assertThrows(JsonException.class, () -> Json.parse("{}x"));
        assertThrows(JsonException.class, () -> Json.parse(""));
    }
}
