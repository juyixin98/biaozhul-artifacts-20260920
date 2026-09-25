package streamagg.test;

import streamagg.json.Json;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/** Tests for the hand-rolled JSON parser/writer. */
public final class JsonTest {

    public static void register(TestRunner r) {
        r.test("json: parses primitives, objects and arrays", () -> {
            Json.JsonValue v = Json.parse("{\"a\":1,\"b\":\"x\",\"c\":true,\"d\":null,\"e\":[1,2,3]}");
            Assert.assertTrue(v.isObject(), "root is object");
            Assert.assertEquals(new BigDecimal("1"), v.get("a").asBigDecimal(), "a");
            Assert.assertEquals("x", v.get("b").asString(), "b");
            Assert.assertTrue(v.get("c").asBoolean(), "c");
            Assert.assertTrue(v.get("d").isNull(), "d");
            List<Json.JsonValue> arr = v.get("e").asArray();
            Assert.assertEquals(3L, arr.size(), "array len");
            Assert.assertEquals(new BigDecimal("2"), arr.get(1).asBigDecimal(), "arr[1]");
        });

        r.test("json: decimal numbers keep exact precision", () -> {
            Json.JsonValue v = Json.parse("{\"v\":0.30}");
            Assert.assertEq(new BigDecimal("0.30"), v.get("v").asBigDecimal(), "exact decimal");
            Json.JsonValue big = Json.parse("{\"v\":123456789012345678901.25}");
            Assert.assertEq(new BigDecimal("123456789012345678901.25"), big.get("v").asBigDecimal(),
                    "large exact decimal");
        });

        r.test("json: string escapes round-trip", () -> {
            Json.JsonValue v = Json.parse("{\"s\":\"a\\nb\\t\\\"c\\\\d\\u00e9\"}");
            Assert.assertEquals("a\nb\t\"c\\d" + "é", v.get("s").asString(), "escapes decoded");
            String out = Json.write(v);
            Assert.assertTrue(out.contains("\\n") && out.contains("\\t") && out.contains("\\\""),
                    "escapes re-encoded: " + out);
        });

        r.test("json: rejects malformed input", () -> {
            String[] bad = {"", "{", "}", "[1,]", "{\"a\":}", "nul", "123abc", "[1 2]", "{a:1}"};
            for (String s : bad) {
                try {
                    Json.parse(s);
                    throw new TestFailure("expected parse failure for: " + s);
                } catch (Json.JsonException expected) {
                    // good
                }
            }
        });

        r.test("json: rejects trailing characters", () -> {
            try {
                Json.parse("{}garbage");
                throw new TestFailure("should reject trailing chars");
            } catch (Json.JsonException expected) {
                // good
            }
        });

        r.test("json: writer produces valid re-parseable output", () -> {
            Map<String, Object> root = new java.util.LinkedHashMap<>();
            root.put("name", "k");
            root.put("sum", new BigDecimal("10.50"));
            root.put("count", 3L);
            root.put("tags", List.of("a", "b"));
            String json = Json.pretty(Json.wrap(root));
            Json.JsonValue reparsed = Json.parse(json);
            Assert.assertEquals("k", reparsed.get("name").asString(), "name");
            Assert.assertEquals(3L, reparsed.get("count").asLong(), "count");
            Assert.assertEquals(2L, reparsed.get("tags").asArray().size(), "tags");
        });
    }
}
