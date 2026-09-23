package com.tjoin.service;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertThrows;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@SuppressWarnings("unchecked")
public class JsonTest {

    @Test
    public void parsesPrimitivesArraysObjects() {
        Map<String, Object> m = (Map<String, Object>) Json.parse(
                "{\"a\": 1, \"b\": -2.5, \"c\": \"x\\ny\", \"d\": true, \"e\": null,"
                        + " \"arr\": [1, 2, 3], \"nested\": {\"k\": \"v\"}}");
        assertEquals(1L, m.get("a"), "long");
        assertEquals(Double.valueOf(-2.5d), m.get("b"), "double");
        assertEquals("x\ny", m.get("c"), "escape");
        assertEquals(Boolean.TRUE, m.get("d"), "bool");
        assertTrue(m.containsKey("e") && m.get("e") == null, "null");
        assertEquals(List.of(1L, 2L, 3L), m.get("arr"), "array");
        assertEquals("v", ((Map<String, Object>) m.get("nested")).get("k"), "nested");
    }

    @Test
    public void roundTrips() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", "R1");
        m.put("ts", 42L);
        m.put("vals", List.of("a", "b"));
        String json = Json.write(m);
        Object back = Json.parse(json);
        assertEquals(m, back, "round trip");
    }

    @Test
    public void rejectsInvalidJson() {
        assertThrows(IllegalArgumentException.class, () -> Json.parse("{bad}"), "garbage");
        assertThrows(IllegalArgumentException.class, () -> Json.parse("[1,2,]"), "trailing comma");
        assertThrows(IllegalArgumentException.class, () -> Json.parse("\"unterminated"), "open string");
    }

    @Test
    public void writesChineseAndEscapes() {
        String json = Json.write(Map.of("msg", "你好\n\t\""));
        Map<String, Object> back = (Map<String, Object>) Json.parse(json);
        assertEquals("你好\n\t\"", back.get("msg"), "unicode + escapes round trip");
    }
}
