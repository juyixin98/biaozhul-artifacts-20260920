package qsummary;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Round-trip and edge parsing tests for the hand-written JSON codec. */
public final class JsonTest {

    private static int checks;

    public static void main(String[] args) {
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("epsilon", 0.01d);
        doc.put("n", 1234567890123L);
        doc.put("values", List.of(1.5d, -2d, 3.25d));
        doc.put("name", "shard-\"A\"\n");
        doc.put("ok", true);
        doc.put("none", null);
        doc.put("nested", Map.of("k", List.of(1L, 2L)));
        String json = Json.write(doc);
        Object back = Json.parse(json);
        check(doc.equals(back), "round trip preserves document");

        check(Long.valueOf(42L).equals(Json.parse("42")), "integer parse");
        check(Double.valueOf(0.5d).equals(Json.parse("0.5")), "double parse");
        check(Boolean.TRUE.equals(Json.parse("true")), "bool parse");
        check(Json.parse("null") == null, "null parse");
        check("a b".equals(Json.parse("\"a b\"")), "string parse");
        check("é".equals(Json.parse("\"\\u00e9\"")), "unicode escape");

        expectBad("{,}", "object bad syntax");
        expectBad("[1,]", "trailing comma");
        expectBad("\"unterminated", "unterminated string");
        expectBad("12 34", "two values");
        expectBad("NaN", "bare NaN");
        expectBad("", "empty");
        System.out.println("JsonTest: ALL PASSED (" + checks + " checks)");
    }

    private static void check(boolean cond, String msg) {
        checks++;
        if (!cond) {
            throw new AssertionError("JsonTest FAIL: " + msg);
        }
    }

    private static void expectBad(String input, String msg) {
        try {
            Json.parse(input);
            throw new AssertionError("JsonTest FAIL: should reject: " + msg);
        } catch (BadRequestException expected) {
            checks++;
        }
    }
}
