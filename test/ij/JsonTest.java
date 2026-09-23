package ij;

import java.util.List;
import java.util.Map;

/** Json 工具的解析与序列化测试。 */
final class JsonTest {

    JsonTest(Assert a) {
        roundTrip(a);
        integersStayLong(a);
        escapes(a);
        rejects(a);
        typedHelpers(a);
    }

    private void roundTrip(Assert a) {
        Map<String, Object> m = Json.parseObject(
                "{\"key\":\"u1\",\"ts\":1234567890123,\"neg\":-5,\"d\":1.5,\"b\":true,\"n\":null,\"arr\":[1,2,{\"x\":\"y\"}]}");
        a.eq(m.get("key"), "u1", "string parse");
        a.eq(m.get("ts"), 1234567890123L, "long parse");
        a.eq(m.get("neg"), -5L, "negative long parse");
        a.eq(m.get("d"), 1.5, "double parse");
        a.eq(m.get("b"), Boolean.TRUE, "boolean parse");
        a.check(m.containsKey("n") && m.get("n") == null, "null parse");
        @SuppressWarnings("unchecked")
        List<Object> arr = (List<Object>) m.get("arr");
        a.eq(arr.size(), 3, "array length");
        String again = Json.write(m);
        a.eq(Json.parseObject(again).get("ts"), 1234567890123L, "round trip preserves long");
    }

    private void integersStayLong(Assert a) {
        Object v = Json.parse("9223372036854775807");
        a.eq(v, Long.MAX_VALUE, "max long stays Long");
        Object v2 = Json.parse("1e3");
        a.check(v2 instanceof Double && ((Double) v2) == 1000.0, "1e3 is double 1000.0");
    }

    private void escapes(Assert a) {
        Object v = Json.parse("\"a\\nb\\tc\\u0041\\\\\\/\"");
        a.eq(v, "a\nb\tcA\\/", "escape decode");
        a.eq(Json.write("中\"文"), "\"中\\\"文\"", "unicode + quote encode");
    }

    private void rejects(Assert a) {
        expectBad(a, "{", "unterminated object");
        expectBad(a, "{'a':1}", "single quote");
        expectBad(a, "[1,2,]", "trailing comma array");
        expectBad(a, "{\"a\":1}x", "trailing chars");
        expectBad(a, "", "empty body");
    }

    private void expectBad(Assert a, String text, String label) {
        try {
            Json.parse(text);
            a.fail(label + " should have failed");
        } catch (Json.JsonException ok) {
            a.check(true, label + " rejected");
        }
    }

    private void typedHelpers(Assert a) {
        Map<String, Object> m = Json.parseObject("{\"s\":\"x\",\"t\":7}");
        a.eq(Json.requireString(m, "s"), "x", "requireString");
        a.eq(Json.requireLong(m, "t"), 7L, "requireLong");
        a.eq(Json.optionalLong(m, "missing", 9), 9L, "optionalLong default");
        try {
            Json.requireLong(m, "s");
            a.fail("requireLong on string should throw");
        } catch (Json.JsonException ok) {
            a.check(true, "requireLong type check");
        }
    }
}
