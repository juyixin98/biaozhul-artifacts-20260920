package seqcep;

import seqcep.json.Json;
import seqcep.json.JsonException;

import java.util.List;
import java.util.Map;

import static seqcep.TestRunner.*;

/** Round-trip and edge-case tests for the minimal JSON layer. */
public final class JsonTests {

    public static void register() {

        test("json round-trip of nested values", () -> {
            Map<String, Object> v = Json.obj(
                    "s", "hello \"world\" \\ \n é",
                    "i", 42L,
                    "neg", -7L,
                    "d", 3.5,
                    "b", true,
                    "n", null,
                    "list", List.of(1L, "two", false),
                    "obj", Map.of("k", "v"));
            String text = Json.write(v);
            Object back = Json.parse(text);
            assertEquals(v, back, "round-trip equality");
        });

        test("json parses escapes and unicode", () -> {
            Object v = Json.parse("\"a\\u0041\\n\\t\\\\\\\"\"");
            assertEquals("aA\n\t\\\"", v, "escape decoding");
        });

        test("json rejects malformed input", () -> {
            assertThrows(JsonException.class, () -> Json.parse("{"), "unterminated object");
            assertThrows(JsonException.class, () -> Json.parse("[1,]"), "trailing comma");
            assertThrows(JsonException.class, () -> Json.parse("{\"a\":1} extra"), "trailing chars");
            assertThrows(JsonException.class, () -> Json.parse("nul"), "bad literal");
            assertThrows(JsonException.class, () -> Json.parse("\"\\x\""), "bad escape");
        });

        test("json integers stay longs", () -> {
            Object v = Json.parse("{\"ts\": 1700000000000}");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) v;
            Object ts = m.get("ts");
            assertTrue(ts instanceof Long, "epoch millis parse as Long, got " + ts.getClass());
            assertEquals(1700000000000L, ((Long) ts).longValue(), "value");
        });
    }

    private JsonTests() {}
}
