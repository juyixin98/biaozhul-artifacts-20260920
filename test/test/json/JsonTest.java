package test.json;

import java.util.List;
import java.util.Map;

import approx.json.Json;
import approx.json.JsonException;
import test.Asserts;
import test.TestRunner;

public final class JsonTest {

    private JsonTest() {
    }

    public static void register(TestRunner r) {
        r.add("json: round trips nested document", JsonTest::testRoundTrip);
        r.add("json: parses escapes and unicode", JsonTest::testEscapes);
        r.add("json: rejects malformed input", JsonTest::testRejectMalformed);
        r.add("json: rejects trailing characters", JsonTest::testTrailing);
        r.add("json: long numbers stay integral", JsonTest::testLongs);
        r.add("json: empty body rejected", () -> {
            try {
                Json.parse("  ");
                Asserts.fail("blank body must fail");
            } catch (JsonException expected) {
                // expected
            }
        });
    }

    private static void testRoundTrip() {
        String text = "{\"name\":\"demo\",\"n\":3,\"f\":1.5,\"b\":true,\"nil\":null,"
                + "\"arr\":[1,2,{\"k\":\"v\"}]}";
        Object parsed = Json.parse(text);
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) parsed;
        Asserts.assertEquals("demo", m.get("name"), "string");
        Asserts.assertEquals(3L, ((Number) m.get("n")).longValue(), "long");
        Asserts.assertEquals(1.5, ((Number) m.get("f")).doubleValue(), "double");
        Asserts.assertEquals(Boolean.TRUE, m.get("b"), "bool");
        Asserts.assertTrue(m.containsKey("nil") && m.get("nil") == null, "null");
        @SuppressWarnings("unchecked")
        List<Object> arr = (List<Object>) m.get("arr");
        Asserts.assertEquals(3, arr.size(), "array size");
        String again = Json.write(parsed);
        Object reparsed = Json.parse(again);
        Asserts.assertEquals(parsed, reparsed, "round-trip stable");
    }

    private static void testEscapes() {
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"a\":\"line1\\nline2\\tu\\u0041\"}");
        Asserts.assertEquals("line1\nline2	uA", m.get("a"), "escapes decoded");
        String written = Json.write(Map.of("q", "\"\\", "nl", "\n"));
        Object back = Json.parse(written);
        @SuppressWarnings("unchecked")
        Map<String, Object> bm = (Map<String, Object>) back;
        Asserts.assertEquals("\"\\", bm.get("q"), "quotes re-escaped");
        Asserts.assertEquals("\n", bm.get("nl"), "newline re-escaped");
    }

    private static void testRejectMalformed() {
        String[] bad = {"{]", "[1,", "{\"a\" 1}", "tru", "01", "[1 2]", "{,}", "\"unterminated"};
        for (String s : bad) {
            try {
                Json.parse(s);
                Asserts.fail("must reject: " + s);
            } catch (JsonException expected) {
                // expected
            }
        }
    }

    private static void testTrailing() {
        try {
            Json.parse("{}garbage");
            Asserts.fail("trailing input must fail");
        } catch (JsonException expected) {
            // expected
        }
    }

    private static void testLongs() {
        long big = 9_000_000_000_000_000_000L; // overflows int
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"v\":" + big + "}");
        Asserts.assertEquals(big, ((Number) m.get("v")).longValue(), "big long preserved");
    }
}
