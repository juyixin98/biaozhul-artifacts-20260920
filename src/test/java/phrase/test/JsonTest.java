package phrase.test;

import phrase.json.Json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 自制 JSON 工具的解析/序列化往返测试。 */
public final class JsonTest {

    public static void register(TestRunner runner) {
        runner.add("json/roundtrip-nested", JsonTest::roundtrip);
        runner.add("json/string-escaping", JsonTest::escaping);
        runner.add("json/numbers-and-literals", JsonTest::numbers);
        runner.add("json/parse-errors", JsonTest::errors);
        runner.add("json/key-order-preserved", JsonTest::order);
    }

    private static void roundtrip() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", "phrase");
        m.put("positions", List.of(2L, 4L, 8L));
        m.put("nested", Map.of("crossField", true, "score", 3.5));
        m.put("nothing", null);
        String json = Json.stringify(m);
        Object back = Json.parse(json);
        Asserts.assertEquals(m, back, "roundtrip preserves values");
    }

    private static void escaping() {
        String raw = "a\"b\\c\n\td";
        String json = Json.stringify(raw);
        Asserts.assertEquals("\"a\\\"b\\\\c\\n\\td\"", json, "escaped string");
        Asserts.assertEquals(raw, Json.parse(json), "unescaped back");
    }

    private static void numbers() {
        Asserts.assertEquals(42L, Json.parse("42"), "integer -> Long");
        Asserts.assertEquals(-7L, Json.parse("-7"), "negative integer");
        Asserts.assertEquals(1.5, Json.parse("1.5"), "double");
        Asserts.assertEquals(Boolean.TRUE, Json.parse("true"), "true");
        Asserts.assertEquals(Boolean.FALSE, Json.parse("false"), "false");
        Asserts.assertEquals(null, Json.parse("null"), "null");
        Asserts.assertEquals(List.of(1L, 2L, 3L), Json.parse("[1,2,3]"), "array");
    }

    private static void errors() {
        Asserts.assertThrows(IllegalArgumentException.class, () -> Json.parse(""), "empty");
        Asserts.assertThrows(IllegalArgumentException.class, () -> Json.parse("{x:1}"),
                "unquoted key");
        Asserts.assertThrows(IllegalArgumentException.class, () -> Json.parse("[1,2]extra"),
                "trailing chars");
        Asserts.assertThrows(IllegalArgumentException.class, () -> Json.parse("[1,2,]"),
                "trailing comma");
    }

    private static void order() {
        Map<?, ?> m = (Map<?, ?>) Json.parse("{\"z\":1,\"a\":2,\"m\":3}");
        Asserts.assertEquals(List.of("z", "a", "m"), List.copyOf(m.keySet()),
                "object key order preserved");
    }
}
