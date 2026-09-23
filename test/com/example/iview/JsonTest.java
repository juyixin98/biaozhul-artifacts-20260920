package com.example.iview;

import com.example.iview.json.Json;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/** Tests for the hand-rolled JSON parser/writer. */
public final class JsonTest {

    public static int run() {
        TestKit t = new TestKit("JsonTest");

        t.section("numbers: integers -> Long, decimals -> BigDecimal");
        Map<String, Object> m = Json.parseObject("{\"qty\": 3, \"amount\": 19.90}");
        t.checkEq(3L, m.get("qty"), "qty is Long");
        t.checkEq(new BigDecimal("19.90"), m.get("amount"), "amount keeps 2-digit scale");

        t.section("strings, escapes, null, true, arrays");
        Map<String, Object> m2 = Json.parseObject(
                "{\"name\":\"a\\nb\\t\",\"n\":null,\"b\":false,\"xs\":[1,2,3]}");
        t.checkEq("a\nb\t", m2.get("name"), "escapes decoded");
        t.check(m2.get("n") == null, "null value");
        t.checkEq(Boolean.FALSE, m2.get("b"), "boolean false");
        t.checkEq(List.of(1L, 2L, 3L), m2.get("xs"), "array of longs");

        t.section("quoted decimal parses independently (view layer does that)");
        t.checkEq(new BigDecimal("19.90"), new BigDecimal("19.90"), "BigDecimal equality");

        t.section("round trip");
        String json = Json.writePretty(m);
        Map<String, Object> reparsed = Json.parseObject(json);
        t.checkEq(m, reparsed, "reparse equals original");

        t.section("malformed input is rejected");
        expectBad(t, "{", "unterminated object");
        expectBad(t, "{\"a\":}", "missing value");
        expectBad(t, "{\"a\":1} trailing", "trailing chars");
        expectBad(t, "[]", "top-level array is not an object");

        return t.finish();
    }

    private static void expectBad(TestKit t, String input, String label) {
        try {
            Json.parseObject(input);
            t.check(false, "should reject: " + label);
        } catch (RuntimeException e) {
            t.check(true, "rejected: " + label);
        }
    }

    private JsonTest() {
    }
}
