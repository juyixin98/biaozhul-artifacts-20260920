package com.example.diff.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Round-trip tests for the minimal JSON parser/writer. */
public final class JsonTests {

    private JsonTests() {
    }

    public static void register(com.example.diff.TestFramework tf) {
        tf.test("round trips nested structures", JsonTests::roundTrip);
        tf.test("parses CRLF and escaped newline inside strings", JsonTests::escapes);
        tf.test("preserves unicode escape", JsonTests::unicode);
        tf.test("rejects trailing garbage", JsonTests::trailing);
        tf.test("writes CRLF text as escaped content", JsonTests::writeCrlf);
    }

    private static void roundTrip() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("a", 1L);
        m.put("b", "two");
        m.put("c", List.of(1L, 2L, 3L));
        m.put("d", true);
        m.put("e", null);
        String json = Json.write(m);
        Object parsed = Json.parse(json);
        com.example.diff.TestFramework.assertEquals(m, parsed);
    }

    private static void escapes() {
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"v\":\"a\\nb\\rc\\td\"}");
        com.example.diff.TestFramework.assertEquals("a\nb\rc\td", m.get("v"));
    }

    private static void unicode() {
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"v\":\"\\u0041\"}");
        com.example.diff.TestFramework.assertEquals("A", m.get("v"));
    }

    private static void trailing() {
        try {
            Json.parse("{}garbage");
            com.example.diff.TestFramework.fail("should reject trailing chars");
        } catch (Json.JsonException expected) {
            // ok
        }
    }

    private static void writeCrlf() {
        String json = Json.write(Map.of("v", "a\r\nb"));
        com.example.diff.TestFramework.assertTrue(json.contains("\\r\\n"),
                "CRLF must be escaped, got " + json);
        @SuppressWarnings("unchecked")
        Map<String, Object> back = (Map<String, Object>) Json.parse(json);
        com.example.diff.TestFramework.assertEquals("a\r\nb", back.get("v"));
    }
}
