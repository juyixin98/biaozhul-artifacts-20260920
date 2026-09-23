package com.example.intervalindex.json;

import com.example.intervalindex.testsupport.Asserts;

import java.util.List;
import java.util.Map;

/** {@link Json} 解析/序列化往返测试。 */
final class JsonTest {

    static void run() {
        testPrimitives();
        testObjectAndArray();
        testRoundTrip();
        testUnicodeAndEscapes();
        testRejectBadJson();
        testLongField();
        System.out.println("JsonTest: all tests passed");
    }

    static void testPrimitives() {
        Asserts.assertEquals(42L, ((Long) Json.parse("42")).longValue(), "parse int");
        Asserts.assertEquals(-7L, Json.parse("-7"), "parse negative");
        Asserts.assertEquals(1.5, Json.parse("1.5"), "parse double");
        Asserts.assertEquals(Boolean.TRUE, Json.parse("true"), "parse true");
        Asserts.assertEquals(Boolean.FALSE, Json.parse("false"), "parse false");
        Asserts.assertEquals(null, Json.parse("null"), "parse null");
        Asserts.assertEquals("hi", Json.parse("\"hi\""), "parse string");
    }

    @SuppressWarnings("unchecked")
    static void testObjectAndArray() {
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"a\":1,\"b\":[true,null,\"x\"]}");
        Asserts.assertEquals(1L, m.get("a"), "object field");
        List<Object> arr = (List<Object>) m.get("b");
        Asserts.assertEquals(3, arr.size(), "array size");
        Asserts.assertEquals(Boolean.TRUE, arr.get(0), "array[0]");
        Asserts.assertEquals(null, arr.get(1), "array[1]");
        Asserts.assertEquals("x", arr.get(2), "array[2]");
        // 空容器与空白容忍
        Asserts.assertEquals(0, ((Map<?, ?>) Json.parse(" { } ")).size(), "empty object");
        Asserts.assertEquals(0, ((List<?>) Json.parse("[\n]")).size(), "empty array");
    }

    static void testRoundTrip() {
        Object v = Json.parse("{\"start\":1,\"end\":5,\"kids\":[1,2,3],\"name\":\"a b\"}");
        String again = Json.stringify(Json.parse(Json.stringify(v)));
        Asserts.assertEquals(Json.stringify(v), again, "round trip stable");
    }

    static void testUnicodeAndEscapes() {
        String s = (String) Json.parse("\"héllo\\n\\u0041\"");
        Asserts.assertEquals("héllo\nA", s, "unicode + escapes");
        String out = Json.stringify("héllo\nA");
        Asserts.assertEquals("\"héllo\\nA\"", out, "escape newline on output");
    }

    static void testRejectBadJson() {
        for (String bad : List.of("", "   ", "{", "[1,]", "{\"a\"}", "tru", "12 3", "[1,2")) {
            boolean failed = false;
            try {
                Json.parse(bad);
            } catch (IllegalArgumentException e) {
                failed = true;
            }
            Asserts.assertTrue(failed, "bad JSON rejected: [" + bad + "]");
        }
    }

    static void testLongField() {
        Object body = Json.parse("{\"start\":3,\"end\":9}");
        Asserts.assertEquals(3L, Json.longField(body, "start").longValue(), "longField start");
        Asserts.assertEquals(9L, Json.longField(body, "end").longValue(), "longField end");
        Asserts.expectThrows("missing field", () -> Json.longField(body, "nope"));
        Asserts.expectThrows("non-object body", () -> Json.longField(List.of(1), "x"));
    }
}
