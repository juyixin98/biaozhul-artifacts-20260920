package com.example.window;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Unit tests for the minimal JSON parser/writer. */
final class JsonTest {

    private JsonTest() {
    }

    static void run() {
        System.out.println("JsonTest");

        Map<String, Object> m = Json.asObject(Json.parse(
                "{\"a\": 1, \"b\": [true, null, \"x\"], \"c\": {\"d\": -2.5}}"));
        TestMain.eq("integer parsed as Long", 1L, m.get("a"));
        List<Object> b = Json.asArray(m.get("b"));
        TestMain.eq("boolean", Boolean.TRUE, b.get(0));
        TestMain.eq("null", null, b.get(1));
        TestMain.eq("string", "x", b.get(2));
        TestMain.eq("double", -2.5, Json.asObject(m.get("c")).get("d"));

        TestMain.eq("string escape round-trip",
                "he\"llo\\world\n",
                Json.parse(Json.write("he\"llo\\world\n")));
        TestMain.eq("unicode escape", "A", Json.parse("\"\\u0041\""));

        Map<String, Object> w = new LinkedHashMap<>();
        w.put("k", 1L);
        w.put("v", null);
        TestMain.eq("write compact", "{\"k\":1,\"v\":null}", Json.write(w));
        TestMain.eq("write array", "[1,2,3]", Json.write(List.of(1L, 2L, 3L)));

        TestMain.expectThrows("malformed object", () -> Json.parse("{bad"));
        TestMain.expectThrows("trailing chars", () -> Json.parse("1 2"));
        TestMain.expectThrows("bad \\u escape", () -> Json.parse("\"\\u00zz\""));
        TestMain.expectThrows("missing field", () -> {
            Map<String, Object> o = Json.asObject(Json.parse("{\"a\":1}"));
            Json.requireString(o, "a");
        });
        TestMain.eq("requireLong on whole double", 5L,
                Json.requireLong(Json.asObject(Json.parse("{\"n\":5.0}")), "n"));
        TestMain.expectThrows("requireLong on fraction", () ->
                Json.requireLong(Json.asObject(Json.parse("{\"n\":5.5}")), "n"));
    }
}
